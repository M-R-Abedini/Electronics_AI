# SPDX-License-Identifier: MIT
"""S6a: deterministic critical copper, selected from the design intent (architecture 7.1, plan F3).

``pixi run s6a <board>`` is the one repeatable entry point.  Nothing is picked
by hand: what gets routed follows from the intent and the board alone.

pairs   every differential pair the intent holds with confidence;
links   every decap with a settled target: its supply pad to the nearest
        target pin on the same layer and net (``plan_power_link``);
taps    every SMD pad of an active part on a net that has a pour on another
        layer: a short track and a through via into that pour
        (``plan_power_tap``).  After the refill KiCad says whether each via
        really sits on filled copper; taps that do not are dropped.

The copper is written, locked, on a copy without other routing.  With
``--c3`` the same placement is route-checked with and without it and the two
coverages are compared: gate C3, coverage with S6a >= without.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
import tempfile
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from .. import report as run_report
from ..intent import model
from ..intent.resolve import resolve
from ..io.kicad import read_board, run_helper
from ..ir import Board, plane_zone_layers
from ..macro import Instance, MTrack, MVia
from ..macro_proof import apply_instance
from ..toolchain import RUNTIME_DIR, sha256_file
from . import drc
from .check import route_check
from .copper import pad_layers
from .fanout import plan_cross_link, plan_power_link, plan_power_tap
from .freerouting import RouterConfig
from .pairs import route_pair, rule_for

LINK_MAX_MM = 5.0  # declared: a decap farther than this from its pin is a placement problem, not a routing one
TAP_MAX_MM = 2.0  # declared: dogbone length; one value for every pad (no per-pad budgets)
MAX_PADS_PER_VIA = 2  # declared: more pads on one plane via raise its inductance for each of them
SHARE_MAX_MM = 1.5  # declared: a pad joins an existing plane via only this close
C3_MARGIN = 0.005  # plan 4.2 gate 2: 0.5 percentage points, until a per-board noise figure is measured
PAIR_ROUNDS = 6  # declared: orderings tried for the pairs (failed pairs move to the front)
ACTIVE_ROLES = ("ic", "converter_ic", "linear_regulator", "power_switch")


def _segments(tracks: list[dict]) -> list[dict]:
    out = []
    for t in tracks:
        pts = [tuple(p) for p in t["points"]]
        out += [{**t, "points": [p, q]} for p, q in itertools.pairwise(pts) if math.dist(p, q) > 1e-6]
    return out


def _route_pairs(board: Board, pairs: list[tuple[str, str]], *, fixed: list[dict], retry_coarse: bool = False) -> dict:
    """Route the pairs one after another; a pair that fails goes to the front of the next round.

    Measured on StickHub: 6 of 8 pairs route alone but only 4 in netlist order, because an earlier pair
    takes the channel a later one needs.  Rounds stop when a round routes everything, repeats an order
    already tried, or after ``PAIR_ROUNDS``.  The best round wins (most pairs, then least copper).
    """
    order, seen, best, rounds = list(pairs), set(), None, []
    for _ in range(PAIR_ROUNDS):
        if tuple(order) in seen:
            break
        seen.add(tuple(order))
        copper, rows, failed, ok = [], {}, [], []
        for pos, neg in order:
            res = route_pair(board, pos, neg, rule_for(board, pos), fixed=fixed + copper, retry_coarse=retry_coarse)
            rows[(pos, neg)] = res.summary()
            if res.failed is None:
                copper += res.tracks + res.vias
                ok.append((pos, neg))
            else:
                failed.append((pos, neg))
        length = sum(sum(r["length_mm"].values()) for r in rows.values())
        rounds.append({"order": [p for p, _n in order], "routed": len(ok)})
        key = (len(ok), -length)
        if best is None or key > best["key"]:
            best = {"key": key, "copper": copper, "rows": [rows[p] for p in pairs]}
        if not failed:
            break
        order = failed + ok
    if best is None:
        return {"copper": [], "rows": [], "rounds": rounds}
    return {"copper": best["copper"], "rows": best["rows"], "rounds": rounds}


def plan(board: Board, intent: dict, *, pairs: bool = True, links: bool = True, taps: bool = True,
         power_first: bool = False, retry_coarse: bool = False) -> dict:
    fixed: list[dict] = []
    report: dict = {"pairs": [], "links": [], "taps": []}
    layers = [cl.canonical for cl in board.copper_layers]

    def route_confirmed_pairs() -> None:
        confirmed = []
        for claim in intent.get("diff_pairs", []):
            pos, neg = claim["value"]["positive"], claim["value"]["negative"]
            if model.is_uncertain(claim):
                report["pairs"].append({"pair": [pos, neg], "routed": False, "failed": "pair not confirmed by the intent"})
            else:
                confirmed.append((pos, neg))
        best = _route_pairs(board, confirmed, fixed=fixed, retry_coarse=retry_coarse)
        report["pairs"].extend(best["rows"])
        report["pair_rounds"] = best["rounds"]
        fixed.extend(best["copper"])

    if pairs and not power_first:
        route_confirmed_pairs()
    # A tap is locked copper with a through via. It is made only for a net that owns an inner plane,
    # which the router does not route on. On a board without one both outer layers carry signals
    # and a locked via blocks them on both (StickHub: one such via cost 56 connections).
    planes = plane_zone_layers(board)
    pours: dict[str, set[str]] = {}
    for zone in board.zones:
        if zone.get("net") and zone.get("outline") and zone.get("uuid") in planes:
            pours.setdefault(zone["net"], set()).update(planes[zone["uuid"]])
    plane_links: list[tuple[dict, tuple[str, str], tuple[str, str]]] = []  # row, cap pad, IC pin: both tap the pour
    linked: set[tuple[str, str]] = set()  # (cap, net) joined to its pin by surface copper
    if links:
        for ref in sorted(intent["parts"], key=model.natural_key):
            part = intent["parts"][ref]
            target = (part.get("target") or {}).get("value") or {}
            if part["role"]["value"] != "decap" or not target.get("ref") or model.is_uncertain(part["target"]):
                continue
            cap, chip = board.fp(ref), board.fp(target["ref"])
            net = target.get("net")
            pins = set(target.get("pins") or [])
            src = next((p for p in cap.pads if p.net == net), None)
            if src is None:
                continue
            sx, sy = cap.pad_world(src)
            cands = sorted((p for p in chip.pads if p.net == net and (not pins or str(p.number) in pins)),
                           key=lambda p: math.dist(chip.pad_world(p), (sx, sy)))
            row = {"cap": ref, "target": target["ref"], "net": net, "routed": False}
            report["links"].append(row)
            if not cands:
                row["failed"] = "target has no pin on the net"
                continue
            same_side = pad_layers(cap, src, layers) == pad_layers(chip, cands[0], layers)
            if not same_side and taps and net in pours:
                # opposite sides of a board with a plane on the net: each end taps the plane (decided after the taps)
                plane_links.append((row, (ref, str(src.number)), (target["ref"], str(cands[0].number))))
                continue
            for pin in cands[:2]:  # nearest pin first; the next one if that pin is boxed in
                if same_side:
                    got = plan_power_link(board, (chip, pin), (cap, src), fixed=fixed, max_length_mm=LINK_MAX_MM)
                else:
                    got = plan_cross_link(board, (chip, pin), (cap, src), fixed=fixed, max_length_mm=LINK_MAX_MM,
                                          tap_max_mm=TAP_MAX_MM)
                if got is not None:
                    fixed += got
                    linked.add((ref, net))
                    row.update(routed=True, pin=str(pin.number), kind="surface" if same_side else "one_via",
                               length_mm=round(sum(math.dist(*t["points"]) for t in _segments(
                                   [c for c in got if "points" in c])), 3))
                    break
            if not row["routed"]:
                row["failed"] = ("no pin within reach on the cap's layer" if same_side
                                 else f"no via site joins the two sides within {LINK_MAX_MM} mm")
    tap_items: list[tuple[dict, dict, dict]] = []
    if taps:
        by_canon = {cl.name: cl.canonical for cl in board.copper_layers}
        users: dict[int, int] = {}  # id(via) -> pads on it
        tap_vias: list[dict] = []
        # ICs first, so a decap on the other side can share its pin's via
        order = [(ref, kind) for kind, roles in (("ic", ACTIVE_ROLES), ("decap", ("decap",)))
                 for ref in sorted(intent["parts"], key=model.natural_key)
                 if intent["parts"][ref]["role"]["value"] in roles]
        for ref, kind in order:
            fp = board.fp(ref)
            for pad in fp.pads:
                if pad.kind != "smd" or pad.net not in pours or (ref, pad.net) in linked:
                    continue
                here = pad_layers(fp, pad, layers)
                targets = sorted({by_canon.get(n, n) for n in pours[pad.net]} - set(here))
                row = {"ref": ref, "pad": str(pad.number), "net": pad.net, "routed": False}
                if kind == "decap":
                    row["kind"] = "decap"
                for layer in targets:
                    share = [v for v in tap_vias if users[id(v)] < MAX_PADS_PER_VIA]
                    got = plan_power_tap(board, fp, pad, layer, fixed=fixed, max_length_mm=TAP_MAX_MM,
                                         share=share, share_max_mm=SHARE_MAX_MM)
                    if got:
                        track, via = got
                        shared = id(via) in users
                        fixed += [track] if shared else [track, via]
                        if not shared:
                            tap_vias.append(via)
                        users[id(via)] = users.get(id(via), 0) + 1
                        tap_items.append((row, track, via))
                        row.update(routed=True, layer=layer, shared_via=shared,
                                   length_mm=round(sum(math.dist(p, q) for p, q in
                                                       itertools.pairwise(track["points"])), 3))
                        break
                if not row["routed"]:
                    row["failed"] = f"no via site within {TAP_MAX_MM} mm" if targets else "pour only on the pad's own layer"
                report["taps"].append(row)
    settle_plane_links(report, plane_links)
    if pairs and power_first:
        route_confirmed_pairs()
    return {"copper": fixed, "report": report, "taps": tap_items, "plane_links": plane_links}


def settle_plane_links(report: dict, plane_links: list) -> None:
    """A decap opposite its IC pin on a plane net is connected when both pads tap the plane."""
    taps = {(r["ref"], r["pad"]): r for r in report["taps"]}
    for row, cap_pad, pin in plane_links:
        ends = {"cap pad": taps.get(cap_pad), "IC pin": taps.get(pin)}
        missing = [name for name, tap in ends.items() if not (tap and tap.get("routed"))]
        row.pop("failed", None)
        if missing:
            row.update(routed=False, failed=f"{missing[0]} has no tap to the plane")
        else:
            row.update(routed=True, kind="plane_coupled", pin=pin[1],
                       shared_via=bool(ends["cap pad"].get("shared_via") or ends["IC pin"].get("shared_via")))


def _instance(copper: list[dict]) -> Instance:
    tracks = [MTrack(t["layer"], t["width"], t["net"], [tuple(p) for p in t["points"]])
              for t in _segments([c for c in copper if "points" in c])]
    vias = [MVia(v["x"], v["y"], v["diameter"], v["drill"], v["net"], tuple(v["layers"])) for v in copper
            if "points" not in v]
    return Instance({}, tracks, vias, [], [], False)


def _filled(board_file: Path, taps: list[tuple[dict, dict, dict]]) -> list[bool]:
    points = [{"x": via["x"], "y": via["y"], "radius": via["drill"] / 2, "net": via["net"], "layer": row["layer"]}
              for row, _track, via in taps]
    with tempfile.TemporaryDirectory(prefix="eai_s6a_") as tmp:
        src, out = Path(tmp) / "points.json", Path(tmp) / "hits.json"
        src.write_text(json.dumps(points), encoding="utf-8")
        run_helper("filled_hits", board_file, src, out)
        return json.loads(out.read_text(encoding="utf-8"))


def _reuse_baseline(path: str | Path, bare: Path, config: RouterConfig) -> dict:
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    if report.get("kind") != "route_check" or not report.get("passed"):
        raise ValueError("C3 baseline must be a passing route-check report")
    expected = {"board": sha256_file(bare)}
    for suffix in (".kicad_pro", ".kicad_dru"):
        sidecar = bare.with_suffix(suffix)
        if sidecar.is_file():
            expected[suffix[1:]] = sha256_file(sidecar)
    actual = {key: value.get("sha256") for key, value in report["inputs"].items()
              if key in ("board", "kicad_pro", "kicad_dru")}
    if actual != expected:
        raise ValueError("C3 baseline board or project rules differ; regenerate the baseline")
    if report["config"] != {"router": asdict(config), "obstacle_keepouts": True}:
        raise ValueError("C3 baseline router settings differ; regenerate it with the same settings")
    tools = run_report.tools()
    for tool, field in (("kicad", "version"), ("java", "version"), ("freerouting", "sha256")):
        if report["tools"][tool].get(field) != tools[tool][field]:
            raise ValueError(f"C3 baseline {tool} differs; regenerate the baseline")
    return report


def run(board_path: str | Path, out_dir: str | Path | None = None, *, intent_path: str | None = None,
        pairs: bool = True, links: bool = True, taps: bool = True, c3: bool = False,
        router_timeout_s: int | None = None, baseline: str | Path | None = None,
        power_first: bool = False, retry_coarse: bool = False) -> dict:
    src = Path(board_path).resolve()
    board = read_board(src)
    intent, intent_info = resolve(board, intent_path)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = (Path(out_dir) if out_dir else RUNTIME_DIR / "s6a" / f"{board.name}-{stamp}").resolve()
    without = None
    if c3:
        bare = run_dir / "bare" / src.name
        apply_instance(src, _instance([]), bare, keep_only=None, strip="tracks")
        if baseline:
            config = RouterConfig(neck_width_um=round(board.rules["min_track_width"] * 1000, 3))
            if router_timeout_s is not None:
                config = replace(config, timeout_s=router_timeout_s)
            without = _reuse_baseline(baseline, bare, config)
    planned = plan(board, intent, pairs=pairs, links=links, taps=taps,
                   power_first=power_first, retry_coarse=retry_coarse)
    copper, report = planned["copper"], planned["report"]
    out = run_dir / "s6a" / src.name
    apply_instance(src, _instance(copper), out, keep_only=None, strip="tracks")
    live_taps = planned["taps"]
    while live_taps:
        hits = _filled(out, live_taps)
        dead = [(row, track, via) for (row, track, via), ok in zip(live_taps, hits, strict=True) if not ok]
        if not dead:
            break
        for row, track, via in dead:
            row.update(routed=False, failed="via is not on filled copper after the refill")
            copper.remove(track)
            if via in copper:  # a shared via goes once, with every tap on it
                copper.remove(via)
        live_taps = [item for item in live_taps if item[0]["routed"]]
        apply_instance(src, _instance(copper), out, keep_only=None, strip="tracks")
    settle_plane_links(report, planned["plane_links"])
    rep = drc.run(out, run_dir / "s6a" / "drc.json")
    nets = {c["net"] for c in copper}
    touching = [v for v in rep.get("violations", [])
                if nets & {drc.item_net(i.get("description", "")) for i in v.get("items", [])}
                and any(drc.is_routing_item(i.get("description", "")) for i in v.get("items", []))]
    counts = {k: {"planned": len(report[k]), "routed": sum(bool(r.get("routed")) for r in report[k])}
              for k in ("pairs", "links", "taps")}
    result = {"board": str(src), "intent": intent_info, "s6a_board": str(out), "counts": counts,
              "copper": {"tracks": sum("points" in c for c in copper), "vias": sum("points" not in c for c in copper)},
              "drc_touching_s6a": [{"type": v["type"], "items": [i.get("description", "") for i in v.get("items", [])]}
                                   for v in touching],
              "report": report,
              "config": {"link_max_mm": LINK_MAX_MM, "tap_max_mm": TAP_MAX_MM, "pairs": pairs, "links": links,
                         "taps": taps, "power_first": power_first, "retry_coarse": retry_coarse}}
    if c3:
        if without is None:
            without = route_check(bare, out_dir=run_dir / "route_without", reference_board=src,
                                  router_timeout_s=router_timeout_s)
        with_ = route_check(out, out_dir=run_dir / "route_with", reference_board=src,
                            router_timeout_s=router_timeout_s)

        a, b = c3_side(without), c3_side(with_)
        result["c3"] = {"without": a, "with": b, **c3_verdict(a, b)}
        if live_taps:
            routed_file = Path(with_["artifacts"]["routed_board"])
            hits = _filled(routed_file, live_taps)
            for (row, _track, _via), ok in zip(live_taps, hits, strict=True):
                row["filled_after_ses"] = ok
            result["c3"]["tap_fill_after_ses"] = {"passed": all(hits), "filled": sum(hits), "total": len(hits)}
    (run_dir / "s6a_report.json").write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    result["report_file"] = str(run_dir / "s6a_report.json")
    return result


def c3_side(r: dict) -> dict:
    m = r["metrics"]
    return {"coverage": m["coverage"], "unrouted": m["unrouted"], "denominator": m["denominator"],
            "unrouted_signal": m.get("unrouted_signal"), "unrouted_plane": m.get("unrouted_plane"),
            "router_stop": m["router_stop"], "router_passes": m.get("router_passes"),
            "pass_unrouted": m.get("pass_unrouted", []),
            "settled": m.get("settled", m["router_stop"] in ("converged", "completed")),
            "router_internal_violations": m.get("router_internal_violations", 0),
            "drc_router_caused": r["stages"]["drc"]["router_caused"],
            "drc_new_non_routing": r["stages"]["drc"]["new_non_routing"]}


def c3_verdict(a: dict, b: dict, margin: float = C3_MARGIN) -> dict:
    """Gate C3 for one board; ``a`` is the run without S6a copper, ``b`` the run with it.

    The runs compare when both settled, or when the baseline settled and the S6a
    run, cut off in the same budget, is already ahead by more than the margin.
    Anything else is inconclusive: ``passed`` is then None, never True.
    """
    clean = a["denominator"] == b["denominator"] and all(
        not r["drc_router_caused"] and not r["drc_new_non_routing"] for r in (a, b))
    basis = None
    if clean and a["settled"] and b["settled"]:
        basis = "both_settled"
    elif clean and a["settled"] and b["coverage"] >= a["coverage"] + margin:
        basis = "baseline_settled_and_s6a_ahead"
    passed = None if basis is None else b["coverage"] >= a["coverage"] - (margin if basis == "both_settled" else 0)
    status = "inconclusive" if passed is None else "passed" if passed else "failed"
    return {"comparable": basis is not None, "basis": basis,
            "gate": {"name": "coverage_with_s6a >= without", "passed": passed, "status": status,
                     "margin": margin}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="s6a", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board")
    ap.add_argument("--out")
    ap.add_argument("--intent", help="design_intent.json (default: drafted from the netlist)")
    for name in ("pairs", "links", "taps"):
        ap.add_argument(f"--no-{name}", action="store_true")
    ap.add_argument("--c3", action="store_true", help="route-check with and without the S6a copper (about 10 minutes)")
    ap.add_argument("--route-timeout", type=int)
    ap.add_argument("--baseline", help="route_without/run_report.json of an earlier --c3 run on the same board")
    ap.add_argument("--power-first", action="store_true", help="reserve local supply copper before routing the pairs")
    ap.add_argument("--coarse-retry", action="store_true", help="try a second grid for failed pairs; slower benchmark")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    res = run(args.board, args.out, intent_path=args.intent, pairs=not args.no_pairs, links=not args.no_links,
              taps=not args.no_taps, c3=args.c3, router_timeout_s=args.route_timeout, baseline=args.baseline,
              power_first=args.power_first, retry_coarse=args.coarse_retry)
    brief = {k: res[k] for k in ("counts", "copper", "s6a_board", "report_file")}
    brief["drc_touching_s6a"] = len(res["drc_touching_s6a"])
    brief["failed"] = {k: [r for r in res["report"][k] if not r.get("routed")][:12] for k in ("pairs", "links", "taps")}
    if "c3" in res:
        brief["c3"] = res["c3"]
    print(json.dumps(brief, indent=1, ensure_ascii=False))
    c3_result = res.get("c3", {})
    passed = (not res["drc_touching_s6a"] and c3_result.get("gate", {}).get("passed", True)
              and c3_result.get("tap_fill_after_ses", {}).get("passed", True))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
