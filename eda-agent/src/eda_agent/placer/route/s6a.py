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
from datetime import UTC, datetime
from pathlib import Path

from ..intent import model
from ..intent.resolve import resolve
from ..io.kicad import read_board, run_helper
from ..ir import Board
from ..macro import Instance, MTrack, MVia
from ..macro_proof import apply_instance
from ..toolchain import RUNTIME_DIR
from . import drc
from .check import route_check
from .copper import pad_layers
from .fanout import plan_power_link, plan_power_tap
from .pairs import route_pair, rule_for

LINK_MAX_MM = 5.0  # declared: a decap farther than this from its pin is a placement problem, not a routing one
TAP_MAX_MM = 2.0  # declared: dogbone length; one value for every pad (no per-pad budgets)
PAIR_ROUNDS = 6  # declared: orderings tried for the pairs (failed pairs move to the front)
ACTIVE_ROLES = ("ic", "converter_ic", "linear_regulator", "power_switch")


def _segments(tracks: list[dict]) -> list[dict]:
    out = []
    for t in tracks:
        pts = [tuple(p) for p in t["points"]]
        out += [{**t, "points": [p, q]} for p, q in itertools.pairwise(pts) if math.dist(p, q) > 1e-6]
    return out


def _route_pairs(board: Board, pairs: list[tuple[str, str]]) -> dict:
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
            res = route_pair(board, pos, neg, rule_for(board, pos), fixed=copper)
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


def plan(board: Board, intent: dict, *, pairs: bool = True, links: bool = True, taps: bool = True) -> dict:
    fixed: list[dict] = []
    report: dict = {"pairs": [], "links": [], "taps": []}
    layers = [cl.canonical for cl in board.copper_layers]
    if pairs:
        confirmed = []
        for claim in intent.get("diff_pairs", []):
            pos, neg = claim["value"]["positive"], claim["value"]["negative"]
            if model.is_uncertain(claim):
                report["pairs"].append({"pair": [pos, neg], "routed": False, "failed": "pair not confirmed by the intent"})
            else:
                confirmed.append((pos, neg))
        best = _route_pairs(board, confirmed)
        report["pairs"] += best["rows"]
        report["pair_rounds"] = best["rounds"]
        fixed += best["copper"]
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
            for pin in cands[:2]:  # nearest pin first; the next one if that pin is boxed in
                got = plan_power_link(board, (chip, pin), (cap, src), fixed=fixed, max_length_mm=LINK_MAX_MM)
                if got:
                    fixed += got
                    row.update(routed=True, pin=str(pin.number),
                               length_mm=round(sum(math.dist(*t["points"]) for t in _segments(got)), 3))
                    break
            if not row["routed"]:
                row["failed"] = ("no pin within reach on the cap's layer" if cands else "target has no pin on the net")
            report["links"].append(row)
    tap_items: list[tuple[dict, dict, dict]] = []
    if taps:
        pours: dict[str, set[str]] = {}
        for zone in board.zones:
            if zone.get("net") and zone.get("outline"):
                pours.setdefault(zone["net"], set()).update(zone["layers"])
        by_canon = {cl.name: cl.canonical for cl in board.copper_layers}
        for ref in sorted(intent["parts"], key=model.natural_key):
            if intent["parts"][ref]["role"]["value"] not in ACTIVE_ROLES:
                continue
            fp = board.fp(ref)
            for pad in fp.pads:
                if pad.kind != "smd" or pad.net not in pours:
                    continue
                here = pad_layers(fp, pad, layers)
                targets = sorted({by_canon.get(n, n) for n in pours[pad.net]} - set(here))
                row = {"ref": ref, "pad": str(pad.number), "net": pad.net, "routed": False}
                for layer in targets:
                    got = plan_power_tap(board, fp, pad, layer, fixed=fixed, max_length_mm=TAP_MAX_MM)
                    if got:
                        track, via = got
                        fixed += [track, via]
                        tap_items.append((row, track, via))
                        row.update(routed=True, layer=layer,
                                   length_mm=round(sum(math.dist(p, q) for p, q in
                                                       zip(track["points"], track["points"][1:], strict=False)), 3))
                        break
                if not row["routed"]:
                    row["failed"] = f"no via site within {TAP_MAX_MM} mm" if targets else "pour only on the pad's own layer"
                report["taps"].append(row)
    return {"copper": fixed, "report": report, "taps": tap_items}


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


def run(board_path: str | Path, out_dir: str | Path | None = None, *, intent_path: str | None = None,
        pairs: bool = True, links: bool = True, taps: bool = True, c3: bool = False,
        router_timeout_s: int | None = None, baseline: str | Path | None = None) -> dict:
    src = Path(board_path).resolve()
    board = read_board(src)
    intent, intent_info = resolve(board, intent_path)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = (Path(out_dir) if out_dir else RUNTIME_DIR / "s6a" / f"{board.name}-{stamp}").resolve()
    planned = plan(board, intent, pairs=pairs, links=links, taps=taps)
    copper, report = planned["copper"], planned["report"]
    out = run_dir / "s6a" / src.name
    apply_instance(src, _instance(copper), out, keep_only=None, strip="tracks")
    if planned["taps"]:  # KiCad decides whether each tap's via is on filled copper
        hits = _filled(out, planned["taps"])
        dead = [(row, track, via) for (row, track, via), ok in zip(planned["taps"], hits, strict=True) if not ok]
        for row, track, via in dead:
            row.update(routed=False, failed="via is not on filled copper after the refill")
            copper.remove(track)
            copper.remove(via)
        if dead:
            apply_instance(src, _instance(copper), out, keep_only=None, strip="tracks")
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
                         "taps": taps}}
    if c3:
        if baseline:  # the same placement's route check without S6a, from an earlier run of this command
            without = json.loads(Path(baseline).read_text(encoding="utf-8"))
        else:
            bare = run_dir / "bare" / src.name
            apply_instance(src, _instance([]), bare, keep_only=None, strip="tracks")
            without = route_check(bare, out_dir=run_dir / "route_without", reference_board=src,
                                  router_timeout_s=router_timeout_s)
        with_ = route_check(out, out_dir=run_dir / "route_with", reference_board=src,
                            router_timeout_s=router_timeout_s)

        def pick(r):
            m = r["metrics"]
            return {"coverage": m["coverage"], "unrouted": m["unrouted"], "denominator": m["denominator"],
                    "router_stop": m["router_stop"],
                    "drc_router_caused": next(g["value"] for g in r["gates"] if g["name"] == "drc_router_caused")}

        a, b = pick(without), pick(with_)
        result["c3"] = {"without": a, "with": b, "gate": {"name": "coverage_with_s6a >= without",
                                                          "passed": b["coverage"] >= a["coverage"]}}
    (run_dir / "s6a_report.json").write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    result["report_file"] = str(run_dir / "s6a_report.json")
    return result


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
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    res = run(args.board, args.out, intent_path=args.intent, pairs=not args.no_pairs, links=not args.no_links,
              taps=not args.no_taps, c3=args.c3, router_timeout_s=args.route_timeout, baseline=args.baseline)
    brief = {k: res[k] for k in ("counts", "copper", "s6a_board", "report_file")}
    brief["drc_touching_s6a"] = len(res["drc_touching_s6a"])
    brief["failed"] = {k: [r for r in res["report"][k] if not r.get("routed")][:12] for k in ("pairs", "links", "taps")}
    if "c3" in res:
        brief["c3"] = res["c3"]
    print(json.dumps(brief, indent=1, ensure_ascii=False))
    return 0 if not res["drc_touching_s6a"] and res.get("c3", {}).get("gate", {}).get("passed", True) else 1


if __name__ == "__main__":
    sys.exit(main())
