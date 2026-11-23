# SPDX-License-Identifier: MIT
"""Strict route check: KiCad -> DSN -> Freerouting -> SES -> refill -> DRC (plan F0a).

The board file given by the user is never written.  Everything happens on
copies inside one run directory, and the result is one ``run_report.json``
(architecture 4.6) whose gates say whether the routed copy may be trusted.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from shapely.geometry import LineString
from shapely.geometry import box as shapely_box
from shapely.ops import unary_union

from .. import report as run_report
from ..eval import legality
from ..io.kicad import KiCadAdapterError, read_board, run_helper
from ..ir import Board, is_ground_net, plane_zone_layers, power_layers
from ..toolchain import RUNTIME_DIR, sha256_file
from . import drc, dsn
from .freerouting import RouterConfig, route
from .freerouting import settled as freerouting_settled

PROJECT_SIDECARS = (".kicad_pro", ".kicad_dru")  # .kicad_prl is per-user view state
MACRO_ZONE_PREFIX = "eai-macro"
TIME_BUDGET_S = 300  # plan F0a acceptance: whole path <= 5 minutes on StickHub
TIME_BUDGET_OVERHEAD_S = 90  # prepare + SES import + zone refill + DRC overhead above router timeout
KICAD_DRC_UNCONNECTED_CAP = 499  # KiCad 10 drc_engine.cpp max reported unconnected_items
MAX_SANITIZE_PASSES = 3


class RouteCheckError(KiCadAdapterError):
    pass


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def stage_input(board: Path, run_dir: Path) -> tuple[Path, dict]:
    """Copy the board and its project sidecars; the project file is mandatory."""
    board = board.resolve()
    if board.suffix != ".kicad_pcb" or not board.is_file():
        raise RouteCheckError(f"not a .kicad_pcb file: {board}")
    project = board.with_suffix(".kicad_pro")
    if not project.is_file():
        raise RouteCheckError(f"{project.name} missing next to the board; without it DRC ignores the "
                              "project rules (architecture 7.3)")
    target = run_dir / "input"
    target.mkdir(parents=True, exist_ok=True)
    copied = target / board.name
    shutil.copyfile(board, copied)
    files = {"board": {"path": str(board), "sha256": sha256_file(board)}}
    for suffix in PROJECT_SIDECARS:
        side = board.with_suffix(suffix)
        if side.is_file():
            shutil.copyfile(side, copied.with_suffix(suffix))
            files[suffix.lstrip(".")] = {"path": str(side), "sha256": sha256_file(side)}
    return copied, files


def keepouts_from(obstacles: list[dict]) -> tuple[list[dsn.Keepout], list[dict]]:
    """DSN keepouts for the obstacles KiCad does not export; skipped ones carry a reason."""
    keep, skipped = [], []
    for row in obstacles:
        reason = ("overlaps pads " + ", ".join(row["overlapping_pads"]) if row["overlapping_pads"]
                  else f"carries net {row['net']}" if row.get("net") else None)
        if reason:
            skipped.append({"uuid": row["uuid"], "kind": row["kind"], "source": row["source"], "reason": reason})
            continue
        for i, outline in enumerate(row["outlines"]):
            keep.append(dsn.Keepout(name=f"{row['kind']}_{row['uuid'][:8]}_{i}", layer=row["copper"],
                                    outline=tuple((float(x), float(y)) for x, y in outline)))
    return keep, skipped


def _by_kind(obstacles: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in obstacles:
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1
    return dict(sorted(counts.items()))


def classify_unrouted(items: list[dict], finished: dict, stop_reason: str) -> list[dict]:
    """One row per DRC ``unconnected_items`` entry, with a cause decided per net.

    KiCad picks which two items of the disconnected islands it reports, and the
    pick depends on item UUIDs that SES import regenerates, so the same session
    can be reported through different items.  The cause therefore uses only
    facts of the net, which the session fixes:

    unattempted    the net has zero attached pads and zero routed vias (not started
                   or fully ripped up due to global congestion / timeout)
    via_blocked    at least one pad escaped, the net has SMD pads on two different
                   copper layers, no through pad, and the router placed no via on it
    escape_blocked at least one pad escaped, while another pad has no copper touching it
    path_blocked   every pad escaped but the router did not join the islands
    The reported items are kept as evidence; the run's stop reason
    (converged / pass_limit / timeout) is attached to every row.
    """
    by_net: dict[str, list[dict]] = {}
    for pad in finished["pads"]:
        by_net.setdefault(pad["net"], []).append(pad)
    vias = finished["routing"].get("vias_by_net", {})
    rows = []
    for entry in items:
        ends = entry.get("items", [])
        descs = [e.get("description", "") for e in ends]
        net = next((n for n in map(drc.item_net, descs) if n is not None), None)
        net_pads = by_net.get(net or "", [])
        smd_layers = {p["layers"][0] for p in net_pads if len(p["layers"]) == 1}
        through = any(len(p["layers"]) > 1 for p in net_pads)
        opposite_sides = len(smd_layers) > 1 and not through
        attached_count = sum(1 for p in net_pads if p["attached"])
        has_via = bool(vias.get(net or "", 0))
        unescaped = sorted(f"{p['ref']}-{p['pad']}" for p in net_pads if not p["attached"])
        if net_pads and attached_count == 0 and not has_via:
            cause = "unattempted"
        elif opposite_sides and not has_via:
            cause = "via_blocked"
        elif unescaped:
            cause = "escape_blocked"
        else:
            cause = "path_blocked"
        rows.append({
            "stage": "S6c",
            "net": net,
            "cause": cause,
            "run_stop": stop_reason,
            "attached_pads": attached_count,
            "total_pads": len(net_pads),
            "opposite_sides": opposite_sides,
            "unescaped_pads": unescaped,
            "evidence": [{"description": d, "pos": e.get("pos")} for d, e in zip(descs, ends)],
        })
    rows.sort(key=lambda r: (r["net"] or "", r["cause"]))
    return rows


def _iter_polys(geom) -> list:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if hasattr(geom, "geoms"):
        out = []
        for g in geom.geoms:
            out.extend(_iter_polys(g))
        return out
    return []


def intent_wire_keepouts(board: Board, intent: dict | None) -> list[dsn.Keepout]:
    """Pad-safe inner-body DSN wire keepouts under crystals and converter inductors on unshielded layers.

    Subtracts every pad on the target layer (buffered by clearance + track width) so a component's own
    pins (including through-hole crystal leads or SMD inductor pads) are never trapped.
    """
    if not intent or not board.copper_layers:
        return []
    ordered_cls = sorted(board.copper_layers, key=lambda cl: cl.index)
    ordered_canons = [cl.canonical for cl in ordered_cls]
    canon_to_name = {cl.canonical: cl.name for cl in ordered_cls}
    name_to_canon = {cl.name: cl.canonical for cl in ordered_cls}
    gnd_planes = {
        name_to_canon.get(r.layer, r.layer)
        for r in board.stackup.roles
        if r.role == "plane" and r.net and is_ground_net(r.net)
    }
    base_clr = max(
        float(board.rules.get("min_clearance") or 0.0),
        max((float(nc.clearance or 0.0) for nc in board.netclasses), default=0.0),
        0.2,
    )
    min_w = max(float(board.rules.get("min_track_width") or 0.0), 0.15)
    lmap = legality.copper_layer_map(board)

    # Pre-build pad geometries per canonical layer for fast exact pad subtraction
    pads_by_canon: dict[str, list] = {c: [] for c in ordered_canons}
    for fp in board.footprints:
        for pad in fp.pads:
            poly = legality._pad_polygon(fp, pad, margin_mm=0.0)
            if poly is None or poly.is_empty:
                continue
            if legality.has_thru_hole_pad(pad):
                for c in ordered_canons:
                    pads_by_canon[c].append(poly)
            else:
                for lyr in pad.layers:
                    c = lmap.get(lyr, lyr)
                    if c in pads_by_canon:
                        pads_by_canon[c].append(poly)

    parts_intent = intent.get("parts") or {}
    keepouts: list[dsn.Keepout] = []
    for fp in board.footprints:
        role_claim = (parts_intent.get(fp.ref) or {}).get("role") or {}
        role = role_claim.get("value")
        if role not in ("crystal", "converter_inductor"):
            continue
        if float(role_claim.get("confidence", 0.0)) < 0.6 and not role_claim.get("confirmed_by_user"):
            continue
        cy = legality.courtyard_polygon(fp)
        if cy is None or cy.is_empty:
            continue
        inner = cy.buffer(-0.2)
        body = inner if (not inner.is_empty and inner.area >= 0.5) else cy
        start_idx = 0 if fp.side == "F" else len(ordered_canons) - 1
        step = 1 if start_idx == 0 else -1
        unshielded: list[str] = []
        idx = start_idx
        while 0 <= idx < len(ordered_canons):
            canon = ordered_canons[idx]
            if idx != start_idx and canon in gnd_planes:
                break
            unshielded.append(canon)
            idx += step
        own_canon = ordered_canons[start_idx]
        for canon in unshielded:
            # For converter inductors on their own side, leave generous room for SW/VOUT entry;
            # on the opposite unshielded layer (e.g. B.Cu under an F.Cu inductor), block the full inner body.
            extra = 0.35 if (role == "converter_inductor" and canon == own_canon) else 0.15
            margin = base_clr + min_w + extra
            bx0, by0, bx1, by1 = body.bounds
            pad_cuts = []
            for p in pads_by_canon.get(canon, ()):
                if not p.intersects(body.buffer(margin)):
                    continue
                pb = p.buffer(margin)
                pad_cuts.append(pb)
                # Cut an open escape channel from every interior pad to the nearest body edge:
                # prevents Specctra DSN polygon holes AND guarantees every pin can escape cleanly.
                c = p.centroid
                px, py = float(c.x), float(c.y)
                exits = [
                    (px - bx0, (bx0 - margin, py)),
                    (bx1 - px, (bx1 + margin, py)),
                    (py - by0, (px, by0 - margin)),
                    (by1 - py, (px, by1 + margin)),
                ]
                _, edge_pt = min(exits, key=lambda item: item[0])
                corridor_half = max(margin, 0.5 * max(p.bounds[2] - p.bounds[0], p.bounds[3] - p.bounds[1]) + margin)
                pad_cuts.append(LineString([(px, py), edge_pt]).buffer(corridor_half, cap_style="flat"))
            safe_geom = body.difference(unary_union(pad_cuts)) if pad_cuts else body
            layer_name = canon_to_name[canon]
            for j, poly in enumerate(_iter_polys(safe_geom)):
                if poly.area < 0.35 or poly.interiors:
                    continue
                ring = [(round(float(x), 4), round(float(y), 4)) for x, y in list(poly.exterior.coords)[:-1]]
                if len(ring) >= 3:
                    keepouts.append(dsn.Keepout(
                        name=f"intent_{role}_{fp.ref}_{canon.replace('.', '_')}_{j}",
                        layer=layer_name,
                        outline=tuple(ring),
                    ))
    return keepouts


def _dru_rule_names(path: Path) -> list[str]:
    """Return sorted custom rule names declared in a KiCad ``.kicad_dru`` file."""
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    return sorted(set(re.findall(r'\(\s*rule\s+"?([^\s"()]+)"?', text)))


def _sanitize_drc_keepouts(violations: list[dict], board: Board, *, half_mm: float = 0.25) -> list[dsn.Keepout]:
    """Build small pad-safe DSN wire keepouts centered on sanitized router DRC locations."""
    if not violations or not board.copper_layers:
        return []
    ordered_cls = sorted(board.copper_layers, key=lambda cl: cl.index)
    name_by_canon = {cl.canonical: cl.name for cl in ordered_cls}
    canon_by_name = {cl.name: cl.canonical for cl in ordered_cls}
    all_canons = [cl.canonical for cl in ordered_cls]
    lmap = legality.copper_layer_map(board)
    pads_by_canon: dict[str, list] = {c: [] for c in all_canons}
    for fp in board.footprints:
        for pad in fp.pads:
            poly = legality._pad_polygon(fp, pad, margin_mm=0.05)
            if poly is None or poly.is_empty:
                continue
            if legality.has_thru_hole_pad(pad):
                for lst in pads_by_canon.values():
                    lst.append(poly)
            else:
                for lyr in pad.layers:
                    c = lmap.get(lyr, lyr)
                    if c in pads_by_canon:
                        pads_by_canon[c].append(poly)

    layer_pat = re.compile(r"\bon\s+([A-Za-z0-9_.\-]+)(?:\s*-\s*([A-Za-z0-9_.\-]+))?")
    seen: set[tuple[str, float, float]] = set()
    keepouts: list[dsn.Keepout] = []
    for v in violations:
        for item in v.get("items", []):
            desc = item.get("description") or ""
            if not drc.is_routing_item(desc):
                continue
            pos = item.get("pos") or {}
            if "x" not in pos or "y" not in pos:
                continue
            x, y = round(float(pos["x"]), 3), round(float(pos["y"]), 3)
            m = layer_pat.search(desc)
            if m:
                a = canon_by_name.get(m.group(1), m.group(1))
                b = canon_by_name.get(m.group(2), m.group(2)) if m.group(2) else a
                if a in all_canons and b in all_canons:
                    ia, ib = all_canons.index(a), all_canons.index(b)
                    target_canons = all_canons[min(ia, ib): max(ia, ib) + 1]
                elif a in all_canons:
                    target_canons = [a]
                else:
                    target_canons = []
            else:
                target_canons = list(all_canons)
            for c in target_canons:
                key = (c, x, y)
                if key in seen:
                    continue
                seen.add(key)
                sq = shapely_box(x - half_mm, y - half_mm, x + half_mm, y + half_mm)
                if any(sq.intersects(p) for p in pads_by_canon.get(c, ())):
                    continue
                ring = (
                    (round(x - half_mm, 4), round(y - half_mm, 4)),
                    (round(x + half_mm, 4), round(y - half_mm, 4)),
                    (round(x + half_mm, 4), round(y + half_mm, 4)),
                    (round(x - half_mm, 4), round(y + half_mm, 4)),
                )
                keepouts.append(dsn.Keepout(
                    name=f"san_drc_{c.replace('.', '_')}_{len(keepouts)}",
                    layer=name_by_canon[c],
                    outline=ring,
                ))
    return keepouts


def _router_item_uuids(router_violations: list[dict]) -> list[str]:
    """Extract deterministic sorted UUIDs of routing items implicated in router-caused DRC findings."""
    uuids: set[str] = set()
    for v in router_violations:
        for item in v.get("items", []):
            uid = item.get("uuid")
            if uid and drc.is_routing_item(item.get("description", "")):
                uuids.add(str(uid))
    return sorted(uuids)


def _starved_thermal_pad_uuids(other_violations: list[dict]) -> list[str]:
    """Extract deterministic sorted pad UUIDs from new starved_thermal DRC violations."""
    uuids: set[str] = set()
    for v in other_violations:
        if v.get("type") != "starved_thermal":
            continue
        for item in v.get("items", []):
            uid = item.get("uuid")
            desc = (item.get("description") or "").lower()
            if uid and "pad" in desc:
                uuids.add(str(uid))
    return sorted(uuids)


def _mask_bridge_aperture_uuids(other_violations: list[dict]) -> list[str]:
    """Extract deterministic sorted .Mask graphic UUIDs from new zone-bridging solder_mask_bridge DRC violations."""
    uuids: set[str] = set()
    for v in other_violations:
        if v.get("type") != "solder_mask_bridge":
            continue
        items = v.get("items", [])
        has_zone = any("zone" in (i.get("description") or "").lower() for i in items)
        if not has_zone:
            continue
        for item in items:
            uid = item.get("uuid")
            desc = item.get("description") or ""
            if uid and (".Mask" in desc) and ("pad" not in desc.lower()):
                uuids.add(str(uid))
    return sorted(uuids)


def route_check(board: str | Path, *, out_dir: str | Path | None = None,
                config: RouterConfig | None = None, neck_width_um: float | None = None,
                obstacle_keepouts: bool = True, reference_board: str | Path | None = None,
                router_timeout_s: int | None = None, intent: dict | None = None,
                sanitize_drc: bool = True, heal_thermals: bool = True,
                heal_mask_bridges: bool = True, allow_router_microvias: bool = False,
                reroute_after_sanitize: bool = True) -> dict:
    """Route-check ``board``.  ``reference_board`` is the original input whose DRC
    findings count as pre-existing (architecture 8.1.3); it defaults to ``board``
    itself, which is right only when ``board`` *is* the designer's file.  A
    candidate placement must pass its original board here, or violations it
    created would be excused as pre-existing."""
    started = time.monotonic()
    board = Path(board)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = (Path(out_dir) if out_dir else RUNTIME_DIR / "route_check" / board.stem / stamp).resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RouteCheckError(f"run directory not empty: {run_dir}")
    work, output = run_dir / "work", run_dir / "output"
    work.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    timings: dict[str, float] = {}

    def tick(name: str, t0: float) -> float:
        now = time.monotonic()
        timings[name] = round(now - t0, 2)
        return now

    t = time.monotonic()
    source, inputs = stage_input(board, run_dir)
    warnings: list[str] = []
    dru_sidecar = source.with_suffix(".kicad_dru")
    if dru_sidecar.is_file():
        dru_rules = _dru_rule_names(dru_sidecar)
        names_str = ", ".join(dru_rules) if dru_rules else dru_sidecar.name
        warnings.append(f"custom .kicad_dru rules are not translated to Specctra DSN: {names_str}")
    reference = drc.run_cached(Path(reference_board) if reference_board else Path(board), work / "drc_input.json")
    inputs["reference_board"] = str(Path(reference_board).resolve()) if reference_board else inputs["board"]["path"]
    t = tick("drc_input", t)

    ir = read_board(source)
    keep = plane_zone_layers(ir)
    # macro zones (architecture 7.3): kept on their own layers so the router sees their copper
    keep |= {z["uuid"]: z["layers"] for z in ir.zones if (z.get("name") or "").startswith(MACRO_ZONE_PREFIX)}
    (work / "plane_zones.json").write_text(json.dumps(keep, indent=1), encoding="utf-8")
    run_helper("prepare", source, work / "plane_zones.json", work / "prepared.kicad_pcb", work / "board.dsn",
               work / "prepare.json")
    prepared = _load(work / "prepare.json")
    if not prepared["dsn_exported"]:
        raise RouteCheckError("KiCad failed to export the Specctra DSN")
    keepouts, skipped = keepouts_from(prepared["obstacles"]) if obstacle_keepouts else ([], [])
    text = (work / "board.dsn").read_text(encoding="utf-8")
    pwr_set = set(power_layers(ir))
    ordered_layer_names = [cl.name for cl in sorted(ir.copper_layers, key=lambda c: c.index)]
    text = dsn.set_layer_types(
        text,
        {cl.name: ("power" if cl.name in pwr_set else "signal") for cl in ir.copper_layers},
    )
    default_nc = next((c for c in ir.netclasses if c.name == "Default"), ir.netclasses[0] if ir.netclasses else None)
    eff_via_d = max(float(getattr(default_nc, "via_diameter", 0.0) or 0.0), float(ir.rules.get("min_via_diameter") or 0.0))
    eff_via_dr = max(float(getattr(default_nc, "via_drill", 0.0) or 0.0), float(ir.rules.get("min_through_drill") or 0.0))
    eff_clr = max(float(getattr(default_nc, "clearance", 0.0) or 0.0), float(ir.rules.get("min_clearance") or 0.0))
    hole_clr_mm = float(ir.rules.get("hole_clearance") or 0.0)
    hole_to_hole_mm = float(ir.rules.get("hole_to_hole") or 0.0)
    edge_clr_mm = float(ir.rules.get("copper_edge_clearance") or 0.0)
    has_vippo_microvias = any(
        v.get("locked") and v.get("via_type") == "micro"
        for v in ir.routing.get("vias", ())
    )
    # When locked VIPPO microvias-in-pad are present, Freerouting natively honors --router.hole_clearance_um
    # without flagging same-net VIPPO pads via injected DSN via_smd clearances.
    via_hole_clrs: dict[str, float] = {}
    if not has_vippo_microvias:
        text, via_hole_clrs = dsn.enforce_via_hole_clearances(
            text,
            hole_clearance_mm=hole_clr_mm,
            hole_to_hole_mm=hole_to_hole_mm,
            via_diameter_mm=eff_via_d,
            via_drill_mm=eff_via_dr,
            copper_clearance_mm=eff_clr,
        )
    macro_zone_keepouts = [dsn.Keepout(name=f"macro_zone_{z['uuid'][:8]}_{i}", layer=layer,
                                       outline=tuple(tuple(q) for q in z["outline"][0]))
                           for z in ir.zones if (z.get("name") or "").startswith(MACRO_ZONE_PREFIX)
                           for i, layer in enumerate(z["layers"])]
    intent_kps = intent_wire_keepouts(ir, intent) if obstacle_keepouts and intent else []
    text = dsn.add_wire_keepouts(text, macro_zone_keepouts + intent_kps)
    text = dsn.restore_wiring_precision(text, ir)
    text, stacked_vias_merged = dsn.merge_stacked_fixed_vias(text, ordered_layer_names)
    text, restricted_vias = dsn.restrict_structure_vias(
        text,
        layer_count=len(ir.copper_layers),
        allow_blind_micro=allow_router_microvias,
    )
    route_dsn = work / "route.dsn"
    route_dsn.write_text(dsn.add_keepouts(text, keepouts), encoding="utf-8", newline="\n")
    t = tick("prepare", t)

    neck = neck_width_um if neck_width_um is not None else round(prepared["min_track_width_mm"] * 1000, 3)
    cfg = replace(config, neck_width_um=neck) if config else RouterConfig(neck_width_um=neck)
    if router_timeout_s is not None:
        cfg = replace(cfg, timeout_s=router_timeout_s)
    if cfg.copper_to_edge_clearance_um is None and edge_clr_mm > 0.0:
        cfg = replace(cfg, copper_to_edge_clearance_um=round(edge_clr_mm * 1000.0, 3))
    if cfg.hole_clearance_um is None and hole_clr_mm > 0.0:
        cfg = replace(cfg, hole_clearance_um=round(hole_clr_mm * 1000.0, 3))
    if has_vippo_microvias and config is None:
        cfg = replace(cfg, strict_drc=False)
    ses = work / "route.ses"
    routed = route(route_dsn, ses, work / "freerouting", cfg)
    t = tick("freerouting", t)
    if routed.session is None:
        raise RouteCheckError(f"Freerouting produced no session (exit {routed.returncode}, "
                              f"stop {routed.stop_reason}); see {work / 'freerouting'}")

    out_board = output / f"{board.stem}.routed.kicad_pcb"
    run_helper("finish", source, ses, out_board, work / "finish.json")
    finished = _load(work / "finish.json")
    for suffix in PROJECT_SIDECARS:
        side = source.with_suffix(suffix)
        if side.is_file() and not out_board.with_suffix(suffix).is_file():
            shutil.copyfile(side, out_board.with_suffix(suffix))
    t = tick("import_refill", t)

    result = drc.run(out_board, work / "drc_routed.json")
    t = tick("drc_routed", t)
    attribution = drc.attribute(result, reference)
    initial_router_drc = attribution.counts()["router_caused"]
    sanitize_info = {
        "passes": 0,
        "removed_tracks": 0,
        "removed_vias": 0,
        "pruned_dangling_tracks": 0,
        "pruned_dangling_vias": 0,
        "initial_router_drc": initial_router_drc,
        "final_router_drc": initial_router_drc,
    }
    sanitized_violations: list[dict] = []
    reroute_budget_s = 0
    if sanitize_drc and initial_router_drc > 0:
        for pass_idx in range(1, MAX_SANITIZE_PASSES + 1):
            sanitized_violations.extend(attribution.router)
            uuids = _router_item_uuids(attribution.router)
            if not uuids:
                break
            rem_file = work / f"sanitize_remove_{pass_idx}.json"
            san_out = work / f"sanitize_{pass_idx}.json"
            rem_file.write_text(json.dumps(uuids, indent=1), encoding="utf-8")
            run_helper("sanitize", out_board, rem_file, out_board, san_out)
            san = _load(san_out)
            if san.get("removed_total", 0) == 0:
                break
            sanitize_info["passes"] = pass_idx
            sanitize_info["removed_tracks"] += int(san["removed_tracks"])
            sanitize_info["removed_vias"] += int(san["removed_vias"])
            sanitize_info["pruned_dangling_tracks"] += int(san.get("pruned_dangling_tracks", 0))
            sanitize_info["pruned_dangling_vias"] += int(san.get("pruned_dangling_vias", 0))
            finished["unrouted_tracks_only"] = san["unrouted_tracks_only"]
            finished["unrouted_final"] = san["unrouted_final"]
            finished["routing"] = san["routing"]
            finished["pads"] = san["pads"]
            result = drc.run(out_board, work / f"drc_sanitized_{pass_idx}.json")
            attribution = drc.attribute(result, reference)
            sanitize_info["final_router_drc"] = attribution.counts()["router_caused"]
            if sanitize_info["final_router_drc"] == 0:
                break
        t = tick("sanitize_drc", t)

        # Single guarded re-route pass for nets opened by sanitize: lock surviving clean copper,
        # place local wire keepouts at removed DRC coordinates, and accept ONLY if DRC stays 0
        # and unrouted strictly drops (zero overhead on clean runs).
        reroute_budget_s = 0
        if (reroute_after_sanitize
                and (sanitize_info["removed_tracks"] + sanitize_info["removed_vias"]) > 0
                and sanitize_info["final_router_drc"] == 0
                and finished["unrouted_final"] > 0):
            rr_dir = work / "sanitize_reroute"
            rr_dir.mkdir(parents=True, exist_ok=True)
            locked_board = rr_dir / source.name
            run_helper("lock_routing", out_board, locked_board, rr_dir / "lock.json")
            for suffix in PROJECT_SIDECARS:
                side = source.with_suffix(suffix)
                if side.is_file():
                    shutil.copyfile(side, locked_board.with_suffix(suffix))
            run_helper("prepare", locked_board, work / "plane_zones.json", rr_dir / "prepared.kicad_pcb",
                       rr_dir / "board.dsn", rr_dir / "prepare.json")
            rr_prep = _load(rr_dir / "prepare.json")
            san_kps = _sanitize_drc_keepouts(sanitized_violations, ir)
            rr_info: dict = {
                "attempted": False,
                "accepted": False,
                "keepouts": len(san_kps),
                "unrouted_before": int(finished["unrouted_final"]),
            }
            if rr_prep.get("dsn_exported"):
                rr_info["attempted"] = True
                rr_text = (rr_dir / "board.dsn").read_text(encoding="utf-8")
                rr_text = dsn.set_layer_types(
                    rr_text,
                    {cl.name: ("power" if cl.name in pwr_set else "signal") for cl in ir.copper_layers},
                )
                if not has_vippo_microvias:
                    rr_text, _ = dsn.enforce_via_hole_clearances(
                        rr_text,
                        hole_clearance_mm=hole_clr_mm,
                        hole_to_hole_mm=hole_to_hole_mm,
                        via_diameter_mm=eff_via_d,
                        via_drill_mm=eff_via_dr,
                        copper_clearance_mm=eff_clr,
                    )
                rr_text, _ = dsn.merge_stacked_fixed_vias(rr_text, ordered_layer_names)
                rr_text = dsn.add_wire_keepouts(rr_text, macro_zone_keepouts + intent_kps + san_kps)
                rr_text, _ = dsn.restrict_structure_vias(
                    rr_text, layer_count=len(ir.copper_layers), allow_blind_micro=allow_router_microvias,
                )
                rr_keepouts, _ = keepouts_from(rr_prep["obstacles"]) if obstacle_keepouts else ([], [])
                rr_dsn = rr_dir / "route.dsn"
                rr_dsn.write_text(dsn.add_keepouts(rr_text, rr_keepouts), encoding="utf-8", newline="\n")
                rr_timeout = min(60, max(20, cfg.timeout_s // 4))
                reroute_budget_s = rr_timeout + 45
                rr_cfg = replace(cfg, timeout_s=rr_timeout)
                rr_ses = rr_dir / "route.ses"
                rr_routed = route(rr_dsn, rr_ses, rr_dir / "freerouting", rr_cfg)
                if rr_routed.session is not None:
                    rr_board = rr_dir / out_board.name
                    run_helper("finish", locked_board, rr_ses, rr_board, rr_dir / "finish.json")
                    rr_fin = _load(rr_dir / "finish.json")
                    for suffix in PROJECT_SIDECARS:
                        side = source.with_suffix(suffix)
                        if side.is_file() and not rr_board.with_suffix(suffix).is_file():
                            shutil.copyfile(side, rr_board.with_suffix(suffix))
                    rr_drc = drc.run(rr_board, rr_dir / "drc.json")
                    rr_attr = drc.attribute(rr_drc, reference)
                    rr_info["unrouted_after"] = int(rr_fin["unrouted_final"])
                    rr_info["router_drc_after"] = int(rr_attr.counts()["router_caused"])
                    rr_info["new_other_after"] = int(rr_attr.counts()["new_non_routing"])
                    if (rr_fin.get("imported")
                            and rr_attr.counts()["router_caused"] == 0
                            and rr_attr.counts()["new_non_routing"] <= attribution.counts()["new_non_routing"]
                            and rr_fin["unrouted_final"] < finished["unrouted_final"]):
                        shutil.copyfile(rr_board, out_board)
                        shutil.copyfile(rr_ses, ses)
                        finished = rr_fin
                        result = rr_drc
                        attribution = rr_attr
                        rr_info["accepted"] = True
            sanitize_info["reroute"] = rr_info
            t = tick("sanitize_reroute", t)

    initial_starved = _starved_thermal_pad_uuids(attribution.new_other)
    thermal_heal_info = {
        "passes": 0,
        "initial_starved": len(initial_starved),
        "relief_modified": 0,
        "solid_modified": 0,
        "final_starved": len(initial_starved),
    }
    if heal_thermals and initial_starved:
        for pass_idx, mode in enumerate(("relief", "solid"), start=1):
            pad_uuids = _starved_thermal_pad_uuids(attribution.new_other)
            if not pad_uuids:
                break
            spec_file = work / f"thermal_heal_{pass_idx}_{mode}.json"
            heal_out = work / f"thermal_heal_{pass_idx}_{mode}_out.json"
            spec_file.write_text(json.dumps({"mode": mode, "pad_uuids": pad_uuids}, indent=1), encoding="utf-8")
            run_helper("heal_thermals", out_board, spec_file, out_board, heal_out)
            healed = _load(heal_out)
            if healed.get("modified_count", 0) == 0:
                break
            thermal_heal_info["passes"] = pass_idx
            thermal_heal_info[f"{mode}_modified"] += int(healed["modified_count"])
            finished["unrouted_tracks_only"] = healed["unrouted_tracks_only"]
            finished["unrouted_final"] = healed["unrouted_final"]
            finished["routing"] = healed["routing"]
            finished["pads"] = healed["pads"]
            result = drc.run(out_board, work / f"drc_thermal_{pass_idx}_{mode}.json")
            attribution = drc.attribute(result, reference)
            thermal_heal_info["final_starved"] = len(_starved_thermal_pad_uuids(attribution.new_other))
            if thermal_heal_info["final_starved"] == 0:
                break
        t = tick("thermal_heal", t)

    initial_mask_bridges = _mask_bridge_aperture_uuids(attribution.new_other)
    mask_heal_info = {
        "applied": False,
        "initial_bridges": len(initial_mask_bridges),
        "added_rule_areas": 0,
        "final_bridges": len(initial_mask_bridges),
    }
    if heal_mask_bridges and initial_mask_bridges:
        spec_file = work / "mask_heal_spec.json"
        heal_out = work / "mask_heal_out.json"
        spec_file.write_text(json.dumps({"mask_uuids": initial_mask_bridges}, indent=1), encoding="utf-8")
        run_helper("heal_mask_bridges", out_board, spec_file, out_board, heal_out)
        healed = _load(heal_out)
        if healed.get("added_count", 0) > 0:
            mask_heal_info["applied"] = True
            mask_heal_info["added_rule_areas"] = int(healed["added_count"])
            finished["unrouted_tracks_only"] = healed["unrouted_tracks_only"]
            finished["unrouted_final"] = healed["unrouted_final"]
            finished["routing"] = healed["routing"]
            finished["pads"] = healed["pads"]
            result = drc.run(out_board, work / "drc_mask_healed.json")
            attribution = drc.attribute(result, reference)
            mask_heal_info["final_bridges"] = len(_mask_bridge_aperture_uuids(attribution.new_other))
        t = tick("mask_bridge_heal", t)

    unrouted_items = [u for u in result.get("unconnected_items", [])]
    failures = classify_unrouted(unrouted_items, finished, routed.stop_reason)
    plane_nets = {z["net"] for z in ir.zones if z.get("uuid") in keep and z.get("net")}
    unrouted_plane = sum(row["net"] in plane_nets for row in failures)

    denominator = prepared["denominator"]
    by_tracks = denominator - finished["unrouted_tracks_only"]
    via_zone = finished["unrouted_tracks_only"] - finished["unrouted_final"]
    unsupported = 0  # no connection class is declared unsupported in F0a
    unrouted = finished["unrouted_final"]
    total_s = round(time.monotonic() - started, 2)
    budget_s = max(TIME_BUDGET_S, cfg.timeout_s + TIME_BUDGET_OVERHEAD_S + reroute_budget_s)
    expected_listed = min(unrouted, KICAD_DRC_UNCONNECTED_CAP)
    final_viols = result.get("violations", [])
    drc_track_dangling = sum(1 for v in final_viols if v.get("type") == "track_dangling")
    drc_via_dangling = sum(1 for v in final_viols if v.get("type") == "via_dangling")
    drc_text_mirror_warnings = sum(
        1 for v in final_viols if v.get("type") in ("mirrored_text_on_front_layer", "nonmirrored_text_on_back_layer")
    )
    metrics = {
        "denominator": denominator,
        "routed_by_tracks": by_tracks,
        "connected_via_zone": via_zone,
        "unsupported": unsupported,
        "unrouted": unrouted,
        "unrouted_plane": unrouted_plane,  # connections of nets that own an inner plane
        "unrouted_signal": len(failures) - unrouted_plane,
        "unrouted_report_truncated": unrouted > KICAD_DRC_UNCONNECTED_CAP,
        "coverage": round((denominator - unrouted) / denominator, 4) if denominator else 1.0,
        "vias": finished["routing"]["vias"],
        "track_length_mm": finished["routing"]["length_mm"],
        "track_width_histogram_mm": finished["routing"]["width_histogram_mm"],
        "session_sha256": sha256_file(ses),
        "router_passes": routed.passes,
        "router_stop": routed.stop_reason,
        "deterministic": routed.stop_reason in ("converged", "completed"),
        "pass_unrouted": routed.pass_unrouted,
        "settled": freerouting_settled(routed.stop_reason, routed.pass_unrouted, unrouted),
        "router_internal_violations": routed.internal_violations,
        "drc_track_dangling": drc_track_dangling,
        "drc_via_dangling": drc_via_dangling,
        "drc_text_mirror_warnings": drc_text_mirror_warnings,
        "seconds_total": total_s,
    }
    gates = [
        run_report.gate("session_imported", finished["imported"], True),
        run_report.gate("drc_router_caused", attribution.counts()["router_caused"], 0, "=="),
        run_report.gate("drc_new_non_routing", attribution.counts()["new_non_routing"], 0, "=="),
        run_report.gate("denominator_stable", finished["denominator"], denominator, "=="),
        run_report.gate("accounting", by_tracks + via_zone + unsupported + unrouted, denominator, "=="),
        run_report.gate("unrouted_listed", len(failures), expected_listed, "=="),
        run_report.gate("time_budget_s", total_s, budget_s, "<="),
    ]
    report = run_report.build(
        kind="route_check",
        inputs=inputs,
        config={"router": asdict(cfg), "obstacle_keepouts": obstacle_keepouts},
        stages={
            "prepare": {k: v for k, v in prepared.items() if k != "obstacles"},
            "layer_roles": [asdict(r) for r in ir.stackup.roles],
            "dsn_power_layers": power_layers(ir),
            "dsn_via_hole_clearances": via_hole_clrs,
            "dsn_stacked_vias_merged": stacked_vias_merged,
            "dsn_restricted_vias": restricted_vias,
            "macro_zone_wire_keepouts": len(macro_zone_keepouts),
            "intent_wire_keepouts": len(intent_kps),
            "obstacle_keepouts": {"exported": len(keepouts), "by_kind": _by_kind(prepared["obstacles"]),
                                  "skipped": skipped},
            "freerouting": routed.summary(),
            "finish": {k: v for k, v in finished.items() if k != "pads"},
            "sanitize": sanitize_info,
            "thermal_heal": thermal_heal_info,
            "mask_bridge_heal": mask_heal_info,
            "drc": attribution.counts(),
        },
        metrics=metrics,
        gates=gates,
        failures=failures,
        drc={"router_caused": attribution.router, "new_non_routing": attribution.new_other,
             "pre_existing": attribution.pre_existing,
             "input_pin_violations_seen_by_router": routed.pre_existing_violations},
        timings=timings,
        warnings=warnings,
        artifacts={"run_dir": str(run_dir), "routed_board": str(out_board), "session": str(ses),
                   "dsn": str(route_dsn)},
    )
    run_report.write(report, run_dir / "run_report.json")
    return report
