# SPDX-License-Identifier: MIT
"""Proof of the macro contract in KiCad (plan F0c): 16 poses, read-back to 1 nm, DRC, Freerouting.

``pixi run macro-proof`` extracts a macro from a real board, writes it on an
otherwise empty copy of that board in 4 rotations x 2 sides x 2 positions
(centre, near the left edge), reads every file back through pcbnew and
compares part pads, tracks, vias and zones with the transform computed by
``macro.instantiate``, and runs KiCad DRC on each.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from shapely.geometry import MultiPoint, Polygon
from shapely.ops import unary_union

from .eval.legality import board_polygon
from .geometry import transform
from .io.kicad import SIDECARS, read_board, run_helper
from .ir import Board
from .macro import (
    Instance,
    Macro,
    MZone,
    Transform,
    extract,
    home,
    instantiate,
    pour_nets,
    predicted_pads,
)
from .route import drc
from .route.check import route_check
from .toolchain import RUNTIME_DIR

READBACK_TOL_MM = 1.5e-6  # plan F0c: 1 nm, plus the rounding of the mm decimal
EDGE_MARGIN_MM = 0.5  # declared: gap kept between the macro and the board edge in the "edge" pose
EDGE_STEP_MM = 0.05


def apply_instance(src: Path, instance: Instance, out: Path, *, keep_only: list[str] | None,
                   strip: str) -> dict:
    out.parent.mkdir(parents=True, exist_ok=True)
    for suffix in SIDECARS:
        if src.with_suffix(suffix).is_file():
            shutil.copyfile(src.with_suffix(suffix), out.with_suffix(suffix))
    spec = {"keep_only": keep_only, "strip": strip, "poses": instance.poses, "lock": True,
            "fill": True, "tracks": [asdict(t) for t in instance.tracks], "vias": [asdict(v) for v in instance.vias],
            "zones": [asdict(z) for z in instance.zones]}
    with tempfile.TemporaryDirectory(prefix="eai_macro_") as tmp:
        spec_path, result = Path(tmp) / "spec.json", Path(tmp) / "result.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        run_helper("macro_apply", src, spec_path, out, result)
        return json.loads(result.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# read-back comparison
# --------------------------------------------------------------------------
def _close(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return len(a) == len(b) and all(abs(x - y) <= READBACK_TOL_MM for x, y in zip(a, b, strict=True))


def _match(expected: list[tuple], actual: list[tuple], key) -> list[str]:
    """Greedy one-to-one matching of geometric records; returns what did not match."""
    left = list(actual)
    misses = []
    for e in expected:
        hit = next((a for a in left if key(e, a)), None)
        if hit is None:
            misses.append(repr(e))
        else:
            left.remove(hit)
    return misses + [f"unexpected {a!r}" for a in left]


def _seg(t: dict | object) -> tuple:
    pts = t["points"] if isinstance(t, dict) else t.points
    layer = t["layer"] if isinstance(t, dict) else t.layer
    net = t["net"] if isinstance(t, dict) else t.net
    flat = tuple(c for q in pts for c in q)
    return layer, net, flat


def readback_diff(source: Board, instance: Instance, written: Board) -> dict:
    misses: dict[str, list[str]] = {"pads": [], "tracks": [], "vias": [], "zones": []}
    for uuid, pose in instance.poses.items():
        fp_src, fp_out = source.by_uuid(uuid), written.by_uuid(uuid)
        expected = predicted_pads(fp_src, pose)
        actual = [(p.number, written_pt) for p, written_pt in fp_out.pads_world()]
        for (n1, q1), (n2, q2) in zip(expected, actual, strict=True):
            if n1 != n2 or not _close(q1, q2):
                misses["pads"].append(f"{fp_src.ref}-{n1}: {q1} != {q2}")
        if fp_out.side != pose["side"]:
            misses["pads"].append(f"{fp_src.ref}: side {fp_out.side} != {pose['side']}")

    def seg_key(e, a):
        if e[0] != a[0] or e[1] != a[1]:
            return False
        if _close(e[2], a[2]):
            return True
        n = len(e[2]) // 2  # a straight segment may come back reversed
        rev = tuple(c for i in range(n - 1, -1, -1) for c in e[2][2 * i: 2 * i + 2])
        return _close(rev, a[2])

    misses["tracks"] = _match([_seg(t) for t in instance.tracks],
                              [_seg(t) for t in written.routing["tracks"] if t["locked"]], seg_key)
    misses["vias"] = _match([(v.net, (v.x, v.y), v.diameter, v.drill, tuple(sorted(v.layers))) for v in instance.vias],
                            [(v["net"], (v["x"], v["y"]), v["diameter"], v["drill"], tuple(sorted(v["layers"])))
                             for v in written.routing["vias"] if v["locked"]],
                            lambda e, a: e[0] == a[0] and _close(e[1], a[1]) and _close(e[2:4], a[2:4]) and e[4] == a[4])
    written_zones = [z for z in written.zones if z.get("name", "").startswith("eai-macro")]
    misses["zones"] = _match([(z.net, tuple(z.layers), tuple(c for q in z.outline[0] for c in q)) for z in instance.zones],
                             [(z["net"], tuple(z["layers"]), tuple(c for q in z["outline"][0] for c in q))
                              for z in written_zones],
                             lambda e, a: e[0] == a[0] and e[1] == a[1] and _close(e[2], a[2]))
    return {k: v[:10] for k, v in misses.items()} | {"ok": not any(misses.values())}


# --------------------------------------------------------------------------
# poses
# --------------------------------------------------------------------------
def instance_hull(source: Board, instance: Instance) -> Polygon:
    pts = []
    for uuid, pose in instance.poses.items():
        fp = source.by_uuid(uuid)
        flip = pose["side"] != fp.side
        for poly in fp.courtyard.polygons:
            pts += [transform((x, -y if flip else y), (pose["x"], pose["y"]), pose["rotation"]) for x, y in poly]
        pts += [q for _n, q in predicted_pads(fp, pose)]
    pts += [tuple(q) for t in instance.tracks for q in t.points] + [(v.x, v.y) for v in instance.vias]
    pts += [tuple(q) for z in instance.zones for ring in z.outline for q in ring]
    return MultiPoint(pts).convex_hull


def poses(macro: Macro, source: Board) -> list[tuple[str, Transform]]:
    outline = board_polygon(source)
    inner = outline.buffer(-(max(source.rules.get("copper_edge_clearance", 0.0), 0.3) + EDGE_MARGIN_MM))
    cx, cy = outline.centroid.x, outline.centroid.y
    out = []
    for side in ("F", "B"):
        for rot in (0.0, 90.0, 180.0, 270.0):
            probe = instance_hull(source, instantiate(macro, Transform(0.0, 0.0, rot, side)))
            ox, oy = round(cx - probe.centroid.x, 3), round(cy - probe.centroid.y, 3)
            if not inner.contains(instance_hull(source, instantiate(macro, Transform(ox, oy, rot, side)))):
                raise ValueError(f"macro does not fit the board at rotation {rot} side {side}")
            out.append((f"{side}{int(rot):03d}_centre", Transform(ox, oy, rot, side)))
            ex = ox
            while inner.contains(instance_hull(source, instantiate(macro, Transform(round(ex - EDGE_STEP_MM, 3), oy,
                                                                                    rot, side)))):
                ex = round(ex - EDGE_STEP_MM, 3)
            out.append((f"{side}{int(rot):03d}_edge", Transform(ex, oy, rot, side)))
    return out


# --------------------------------------------------------------------------
# test macro with a local zone (SW-node-like polygon)
# --------------------------------------------------------------------------
def add_local_zone(macro: Macro, source: Board, net: str, name: str, grow_mm: float = 0.35) -> Macro:
    """Give ``macro`` a local copper zone of ``net``: the hull of that net's pad shapes, grown by ``grow_mm``."""
    pts, layer = [], None
    for part in macro.parts:
        fp = source.by_uuid(part.uuid)
        for pad in fp.pads:
            if pad.net == net:
                w, h = pad.size
                for corner in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
                    local = transform(corner, pad.local, pad.rotation)
                    pts.append(transform(local, (part.x, part.y), part.rotation))
                layer = layer or (pad.layers[0] if pad.layers else "F.Cu")
    if len(pts) < 2:
        raise ValueError(f"net {net} has fewer than two pads in the macro")
    shape = unary_union([MultiPoint(pts).convex_hull.buffer(grow_mm, join_style="mitre")]).simplify(0.01)
    ring = [(round(x, 4), round(y, 4)) for x, y in list(shape.exterior.coords)[:-1]]
    macro.zones.append(MZone(net=net, layers=[layer], outline=[ring], priority=10, name=name))
    return macro


# --------------------------------------------------------------------------
# proof
# --------------------------------------------------------------------------
def prove(board_path: str | Path, refs: list[str], out_dir: str | Path, *, template: str,
          zone_net: str | None = None) -> dict:
    src = Path(board_path).resolve()
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    source = read_board(src)
    macro = extract(source, refs, template=template)
    if zone_net:
        add_local_zone(macro, source, zone_net, f"eai-macro:{template}")
    (out / "macro.json").write_text(json.dumps(macro.to_dict(), indent=1), encoding="utf-8")
    keep = [p.uuid for p in macro.parts]
    macro_nets = {pad.net for p in macro.parts for pad in source.by_uuid(p.uuid).pads if pad.net}
    signal_nets = macro_nets - pour_nets(source)
    rows = []
    for name, t in poses(macro, source):
        inst = instantiate(macro, t)
        target = out / f"{name}.kicad_pcb"
        applied = apply_instance(src, inst, target, keep_only=keep, strip="all")
        written = read_board(target)
        diff = readback_diff(source, inst, written)
        report = drc.run(target, out / f"{name}.drc.json")
        errors = [v for v in report.get("violations", []) if v.get("severity") == "error"]
        # every footprint left on the board is a macro part, so an unconnected item of a
        # signal net (not reaching the rest through a pour) is a broken internal connection
        internal = [u for u in report.get("unconnected_items", [])
                    if (n := drc.item_net(u["items"][0]["description"])) and n in signal_nets]
        rows.append({"pose": name, "transform": asdict(t), "applied": applied, "readback": diff,
                     "drc_errors": len(errors),
                     "drc_error_types": sorted({v["type"] for v in errors}),
                     "internal_unconnected": len(internal),
                     "passed": diff["ok"] and not errors and not internal})
    summary = {"board": str(src), "template": template, "parts": refs, "checks": [asdict(c) for c in macro.checks],
               "ports": [asdict(p) for p in macro.ports], "copper": {"tracks": len(macro.tracks),
                                                                     "vias": len(macro.vias), "zones": len(macro.zones)},
               "poses_passed": sum(r["passed"] for r in rows), "poses": len(rows), "rows": rows}
    (out / "proof.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


def _locked_copper(board: Board) -> dict:
    tracks = sorted((t["layer"], t["net"], t["width"], tuple(map(tuple, t["points"])))
                    for t in board.routing["tracks"] if t["locked"])
    vias = sorted((v["net"], v["x"], v["y"], v["diameter"], v["drill"], tuple(v["layers"]))
                  for v in board.routing["vias"] if v["locked"])
    zones = sorted((z["net"], tuple(z["layers"]), tuple(tuple(q) for q in z["outline"][0]),
                    tuple(sorted(z["filled_area_mm2"].items())))
                   for z in board.zones if (z.get("name") or "").startswith("eai-macro"))
    return {"tracks": tracks, "vias": vias, "zones": zones}


def route_with_macros(board_path: str | Path, specs: list[tuple[str, list[str], str | None]],
                      out_dir: str | Path, *, router_timeout_s: int | None = None) -> dict:
    """Plan F0c task 5: macros at their home pose on the real board, Freerouting the rest.

    Gates: macro copper (tracks, vias, local zones) identical before and after
    routing, zero router-caused DRC, and coverage with macros >= without.
    """
    src = Path(board_path).resolve()
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    source = read_board(src)
    current = src
    macros = []
    for i, (template, refs, zone_net) in enumerate(specs):
        macro = extract(source, refs, template=template)
        if zone_net:
            add_local_zone(macro, source, zone_net, f"eai-macro:{template}")
        macros.append(macro)
        target = out / f"with_macros_{i}.kicad_pcb"
        apply_instance(current, instantiate(macro, home(macro)), target, keep_only=None,
                       strip="tracks" if i == 0 else "none")
        current = target
    before = _locked_copper(read_board(current))
    with_macros = route_check(current, out_dir=out / "route_with", reference_board=src, router_timeout_s=router_timeout_s)
    after = _locked_copper(read_board(with_macros["artifacts"]["routed_board"]))
    without = route_check(src, out_dir=out / "route_without", router_timeout_s=router_timeout_s)
    identical = {k: before[k] == after[k] for k in before}
    cov_with, cov_without = with_macros["metrics"]["coverage"], without["metrics"]["coverage"]
    summary = {
        "board": str(src),
        "macros": [{"template": m.template, "parts": [p.ref for p in m.parts], "tracks": len(m.tracks),
                    "vias": len(m.vias), "zones": len(m.zones), "checks": [asdict(c) for c in m.checks]}
                   for m in macros],
        "locked_copper": {k: len(v) for k, v in before.items()},
        "copper_identical_after_routing": identical,
        "fixed_reconcile": with_macros["stages"]["finish"].get("fixed"),
        "route_with": {k: with_macros["metrics"][k] for k in ("coverage", "unrouted", "vias", "router_stop")}
        | {"failed_gates": [g["name"] for g in with_macros["gates"] if not g["passed"]],
           "drc": with_macros["stages"]["drc"]},
        "route_without": {k: without["metrics"][k] for k in ("coverage", "unrouted", "vias", "router_stop")},
        "gates": {"copper_identical": all(identical.values()),
                  "router_drc_clean": with_macros["stages"]["drc"]["router_caused"] == 0
                  and with_macros["stages"]["drc"]["new_non_routing"] == 0,
                  "coverage_with_ge_without": cov_with >= cov_without},
    }
    summary["passed"] = all(summary["gates"].values())
    (out / "route_proof.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="macro-proof", description=__doc__)
    ap.add_argument("board")
    ap.add_argument("--refs", required=True, help="comma-separated references of the macro parts")
    ap.add_argument("--template", default="extracted")
    ap.add_argument("--zone-net", help="add a local copper zone of this net (zone test macro)")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    out = Path(args.out) if args.out else RUNTIME_DIR / "macro_proof" / args.template
    s = prove(args.board, args.refs.split(","), out, template=args.template, zone_net=args.zone_net)
    print(json.dumps({k: v for k, v in s.items() if k != "rows"} | {
        "rows": [{k: r[k] for k in ("pose", "passed", "drc_errors", "drc_error_types", "internal_unconnected")} | {"readback_ok": r["readback"]["ok"]}
                 for r in s["rows"]]}, indent=1))
    return 0 if s["poses_passed"] == s["poses"] else 1


if __name__ == "__main__":
    sys.exit(main())
