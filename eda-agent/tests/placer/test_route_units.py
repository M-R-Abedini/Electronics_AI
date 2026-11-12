# SPDX-License-Identifier: MIT
"""Unit tests of the F0a route-check pieces that need no KiCad or Java."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest
from eda_agent.placer.ir import (
    Board,
    CopperLayer,
    Footprint,
    Net,
    NetClass,
    Outline,
    Pad,
)
from eda_agent.placer.route import drc, dsn, pairs
from eda_agent.placer.route.check import classify_unrouted
from eda_agent.placer.route.fanout import plan_power_link, plan_power_tap
from eda_agent.placer.route.freerouting import RouterConfig, parse_log

DSN = """(pcb x.dsn
  (resolution um 10)
  (unit um)
  (structure
    (layer F.Cu (type signal))
    (layer B.Cu (type signal))
    (boundary (path pcb 0 0 0 1000 0))
    (via "Via[0-1]_500:300_um")
    (rule (width 150))
  )
)
"""


def test_keepout_inserted_before_via_with_y_negated():
    k = dsn.Keepout("mask_a", "F.Cu", ((1.0, 2.0), (1.5, 2.0), (1.5, 2.25)))
    out = dsn.add_keepouts(DSN, [k])
    assert '(keepout "mask_a" (polygon F.Cu 0 1000 -2000 1500 -2000 1500 -2250))' in out
    assert out.index("(keepout") < out.index('(via "Via')
    assert out.replace(dsn.keepout_text(k, 1000.0), "") == DSN


@pytest.mark.parametrize(("last_failure", "skew", "neg_vias", "max_vias", "error"), [
    ("breakout blocked", 0.0, 1, 2, "breakout blocked"),
    (None, 0.1504, 1, 2, "pair skew"),
    (None, 0.0, 0, 2, "unequal via counts"),
    (None, 0.0, 1, 0, "vias per leg"),
    (None, 0.1, 1, 2, None),
])
def test_pair_only_exposes_copper_for_a_complete_pair(monkeypatch, last_failure, skew, neg_vias, max_vias, error):
    monkeypatch.setitem(sys.modules, "placer_core", ModuleType("placer_core"))
    monkeypatch.setattr(pairs, "terminals", lambda *_: [1, 2, 3])
    monkeypatch.setattr(pairs, "chain", lambda terms: terms)

    def segment(_board, start, end, *_):
        info = {"from": start, "to": end}
        if end == 3 and last_failure:
            return {"failed": last_failure, "info": info}
        length = 1.0 + (skew if end == 3 else 0.0)
        return {
            "failed": None,
            "info": info,
            "tracks": [
                {"net": "D+", "layer": "F.Cu", "width": 0.15, "points": [(0, 0), (length, 0)]},
                {"net": "D-", "layer": "F.Cu", "width": 0.15, "points": [(0, 1), (1, 1)]},
            ],
            "vias": [
                {"net": net, "x": 1, "y": y, "diameter": 0.5, "drill": 0.3, "layers": ["F.Cu", "B.Cu"]}
                for net, y in ([("D+", 0)] + [("D-", 1)] * neg_vias)
            ] if end == 3 else [],
        }

    monkeypatch.setattr(pairs, "_route_segment", segment)
    parts = [
        Footprint(f"J{i}", "", "", 0, i, 0, pads=[
            Pad("1", "D+", (0, 0), (0.3, 0.3)),
            Pad("2", "D-", (2.54, 0), (0.3, 0.3)),
        ])
        for i in (1, 2, 3)
    ]
    rule = pairs.PairRule(0.15, 0.15, 0.15, 0.5, 0.3, max_vias_per_leg=max_vias)
    result = pairs.route_pair(Board("pair", parts), "D+", "D-", rule)
    assert len(result.segments) == 2
    if error:
        assert result.failed is not None and error in result.failed
        assert not result.tracks and not result.vias
    else:
        assert result.failed is None
        assert len(result.tracks) == 4 and len(result.vias) == 2


def test_pair_keeps_wide_connector_terminals():
    connector = Footprint("J1", "", "", 0, 0, 0, pads=[
        Pad("2", "D-", (0, 0), (0.8, 0.8)),
        Pad("3", "D+", (2.54, 0), (0.8, 0.8)),
    ])
    board = Board("pair", [connector], copper_layers=[CopperLayer("F.Cu", "F.Cu", 0)])
    assert [t.refs for t in pairs.terminals(board, "D+", "D-")] == ["J1"]


def test_breakout_joins_exact_endpoints_and_checks_partner_clearance():
    layer = "F.Cu"
    win = pairs.Window(Board("pair", []), (0, 0, 1, 1), [layer])
    raw = {layer: np.zeros_like(win.X, dtype=bool)}
    core = SimpleNamespace(grid_astar=lambda _nx, _ny, _nl, _blocked, _via, starts, ends, *_: starts + ends)
    start, end = ((0.012, 0.21), layer), ((0.823, 0.21), layer)
    rule = pairs.PairRule(0.15, 0.15, 0.15, 0.5, 0.3)

    result = pairs._breakout(win, raw, [layer], [], start, end, "D+", rule, core, False)
    assert result is not None
    tracks, vias = result
    assert not vias
    assert tracks[0]["points"] == [start[0], end[0]]

    partner = {"net": "D-", "layer": layer, "width": 0.15, "points": [(0, 0.49), (1, 0.49)]}
    assert pairs._breakout(win, raw, [layer], [partner], start, end, "D+", rule, core, False) is None


def test_same_net_via_holes_obey_drill_clearance():
    win = pairs.Window(Board("pair", []), (0, 0, 2, 2), ["F.Cu"])
    mask = np.zeros_like(win.X, dtype=bool)
    holes = [(0.5, 0.5, 0.3)]
    pairs._block_drills(win, mask, holes, 0.3, 0.25)

    for x, blocked in [(1.0, True), (1.05, False), (1.1, False)]:
        i, j = win.cell(x, 0.5)
        assert bool(mask[j, i]) == blocked
        via = {"net": "D+", "x": x, "y": 0.5, "drill": 0.3}
        assert pairs._holes_separated([via], holes, 0.25) == (not blocked)


def test_tuning_can_extend_two_segments_of_one_track():
    layer = "F.Cu"
    win = pairs.Window(Board("tune", []), (0, 0, 15, 10), [layer])
    mask = np.zeros_like(win.X, dtype=bool)
    tracks = [
        {"net": "P", "layer": layer, "width": 0.15, "points": [(1, 2), (5, 2), (5, 5)]},
        {"net": "N", "layer": layer, "width": 0.15, "points": [(1, 8), (12.5, 8)]},
    ]
    rule = pairs.PairRule(0.15, 0.15, 0.15, 0.5, 0.3)
    tuned, skew = pairs._tune(win, {"pos": {layer: mask}, "neg": {layer: mask}},
                              tracks, {"pos": "P", "neg": "N"}, rule, 4.5)

    assert skew <= rule.skew_max
    assert tuned[0]["points"][0] == (1, 2) and tuned[0]["points"][-1] == (5, 5)
    assert any(x != 5 and y > 2 for x, y in tuned[0]["points"])


def test_power_tap_reaches_matching_pour_and_respects_existing_drill():
    outline = [(0, 0), (10, 0), (10, 10), (0, 10)]
    pad = Pad("1", "GND", (0.5, 0), (0.4, 0.3), layers=("B.Cu",))
    chip = Footprint("U1", "", "", 4, 4, 0, side="B", pads=[pad])
    board = Board("power", [chip], outline=[Outline(outline)],
                  copper_layers=[CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("B.Cu", "B.Cu", 1)],
                  nets=[Net("GND", "Default", 1)], netclasses=[NetClass("Default", 0.15, 0.2, 0.5, 0.3)],
                  zones=[{"net": "GND", "layers": ["F.Cu"], "outline": [outline]}],
                  rules={"hole_to_hole": 0.25})

    result = plan_power_tap(board, chip, pad, "F.Cu", fixed=[])
    assert result is not None
    track, via = result
    assert track["width"] == 0.2 and track["layer"] == "B.Cu"
    assert track["points"] == [chip.pad_world(pad), (via["x"], via["y"])]
    rerouted = plan_power_tap(board, chip, pad, "F.Cu", fixed=[via])
    assert rerouted is not None
    assert pairs._holes_separated([rerouted[1]], [(via["x"], via["y"], via["drill"])], 0.25)

    board.zones[0]["net"] = "VCC"
    assert plan_power_tap(board, chip, pad, "F.Cu", fixed=[]) is None


def test_power_tap_can_bend_around_track_keepout():
    outline = [(0, 0), (6, 0), (6, 6), (0, 6)]
    pour = [(2.74, 2.74), (3.26, 2.74), (3.26, 3.26), (2.74, 3.26)]
    blocked = [(2.18, 2.18), (2.48, 2.18), (2.48, 2.48), (2.18, 2.48)]
    pad = Pad("1", "GND", (0.5, 0), (0.4, 0.3), layers=("B.Cu",))
    chip = Footprint("U1", "", "", 1.5, 2, 0, side="B", pads=[pad])
    board = Board("bent-tap", [chip], outline=[Outline(outline)],
                  copper_layers=[CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("B.Cu", "B.Cu", 1)],
                  nets=[Net("GND", "Default", 1)], netclasses=[NetClass("Default", 0.15, 0.2, 0.5, 0.3)],
                  zones=[{"net": "GND", "layers": ["F.Cu"], "outline": [pour]}],
                  rule_areas=[{"layers": ["B.Cu"], "polygons": [blocked],
                               "keepout": {"tracks": True, "vias": False}}])

    result = plan_power_tap(board, chip, pad, "F.Cu", fixed=[])
    assert result is not None
    track, via = result
    assert np.allclose(track["points"], [(2, 2), (3, 2), (3, 3)])
    assert np.allclose((via["x"], via["y"]), (3, 3))


def test_power_link_checks_width_against_keepout_after_search(monkeypatch):
    core = SimpleNamespace(grid_astar=lambda _nx, _ny, _nl, _blocked, _via, starts, ends, *_: starts + ends)
    monkeypatch.setitem(sys.modules, "placer_core", core)
    p = Pad("8", "VCC", (0, 0), (0.4, 0.3), layers=("B.Cu",))
    q = Pad("1", "VCC", (0, 0), (0.4, 0.3), layers=("B.Cu",))
    chip = Footprint("U1", "", "", 2, 2, 0, side="B", pads=[p])
    cap = Footprint("C1", "", "", 4, 2, 0, side="B", pads=[q])
    board = Board("local-power", [chip, cap], outline=[Outline([(0, 0), (6, 0), (6, 6), (0, 6)])],
                  copper_layers=[CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("B.Cu", "B.Cu", 1)],
                  nets=[Net("VCC", "Power", 2)], netclasses=[NetClass("Power", 0.15, 0.2, 0.5, 0.3)])

    track, = plan_power_link(board, (chip, p), (cap, q), fixed=[])
    assert track == {"net": "VCC", "layer": "B.Cu", "width": 0.2, "points": [(2, 2), (4, 2)]}
    core.grid_astar = lambda _nx, _ny, _nl, _blocked, _via, starts, ends, *_: (
        starts + ends if starts[0][0] > ends[0][0] else None
    )
    assert plan_power_link(board, (chip, p), (cap, q), fixed=[]) == [track]
    board.rule_areas.append({"layers": ["B.Cu"], "polygons": [[(3, 2.06), (3.5, 2.06), (3.5, 3), (3, 3)]],
                             "keepout": {"tracks": True}})
    assert plan_power_link(board, (chip, p), (cap, q), fixed=[]) is None


def test_keepout_quotes_unusual_layer_names_and_noop_when_empty():
    k = dsn.Keepout("m", "Top Layer", ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0)))
    assert '(polygon "Top Layer" 0' in dsn.keepout_text(k, 1000.0)
    assert dsn.add_keepouts(DSN, []) == DSN
    assert dsn.layers(DSN) == ["F.Cu", "B.Cu"]


def test_router_flags_are_the_architecture_configuration():
    flags = RouterConfig(neck_width_um=150).flags()
    assert "--router.automatic_neckdown=false" in flags
    assert "--router.fanout.enabled=false" in flags
    assert "--router.neck_width_um=150" in flags
    assert "--router.strict_drc=true" in flags
    assert "--router.optimizer.max_threads=1" in flags
    assert "--router.job_timeout=00:10:00" in flags
    assert "--router.job_timeout=01:02:05" in RouterConfig(neck_width_um=150, timeout_s=3725).flags()


def test_parse_log_reads_passes_stop_and_pin_violations():
    log = (
        "  - 1 unfixable violation(s):\n      * 1 pin-to-pin clearance violations:\n"
        "          - JP1.2 [net VIN] <-> JP1.1 [net Net-(J1-Pin_1)]\n"
        "Auto-routing pass #1 on board 'a' was completed\nAuto-routing pass #19 on board 'b'\n"
        "The router's best score (940.94) has not improved by more than 0.5 points since pass #9.\n"
        "Auto-routing stage completed: started with 226 unrouted nets, final score: 940.94 (10 unrouted and 1 violation)\n"
    )
    got = parse_log(log)
    assert got["passes"] == 19 and got["stop_reason"] == "converged" and got["log_unrouted"] == 10
    assert got["pre_existing_violations"] == ["JP1.2 [net VIN] <-> JP1.1 [net Net-(J1-Pin_1)]"]


def test_parse_log_reads_graceful_timeout():
    got = parse_log("Auto-routing pass #1 on board 'a'\nJob 'x' finished with state: TIMED_OUT (elapsed: 1 minute)\n")
    assert got["stop_reason"] == "timeout"


def _v(kind, *descs, severity="error"):
    return {"type": kind, "severity": severity, "items": [{"description": d} for d in descs]}


def test_drc_attribution_separates_router_preexisting_and_new():
    reference = {"violations": [_v("clearance", "Pad 1 [VIN] of JP1", "Pad 2 [X] of JP1")]}
    routed = {"violations": [
        _v("clearance", "Pad 1 [VIN] of JP1", "Pad 2 [X] of JP1"),        # pre-existing
        _v("track_width", "Track [GND] on F.Cu, length 1 mm"),             # router
        _v("solder_mask_bridge", "Polygon on F.Mask", "Via [GND] on F.Cu - B.Cu"),  # router
        _v("clearance", "Pad 1 [A] of R1", "Zone [GND] on F.Cu"),          # new, not routing
        _v("lib_footprint_issues", "Footprint R1", severity="warning"),
    ]}
    a = drc.attribute(routed, reference)
    c = a.counts()
    assert (c["router_caused"], c["pre_existing"], c["new_non_routing"], c["warnings"]) == (2, 1, 1, 1)
    assert c["failing_by_type"] == {"clearance": 1, "solder_mask_bridge": 1, "track_width": 1}


def test_unrouted_causes_use_net_facts_not_reported_items():
    def pad(uuid, net, ref, layers, attached):
        return {"uuid": uuid, "net": net, "ref": ref, "pad": "1", "layers": layers, "attached": attached}

    finished = {
        "routing": {"vias_by_net": {"N2": 1}},
        "pads": [
            pad("a1", "N1", "U1", ["B.Cu"], False), pad("a2", "N1", "J1", ["F.Cu"], True),
            pad("b1", "N2", "U1", ["B.Cu"], False), pad("b2", "N2", "J2", ["F.Cu"], True),
            pad("c1", "N3", "R1", ["F.Cu"], True), pad("c2", "N3", "J3", ["F.Cu", "B.Cu"], True),
        ],
    }

    def entry(net, first):
        return {"items": [{"description": first, "uuid": "x"},
                          {"description": f"Pad 2 [{net}] of Y", "uuid": "y"}]}

    # The same net reported through a track or through a pad gets the same cause.
    for first in ("Track [{}] on F.Cu, length 1.4 mm", "Pad 1 [{}] of U1 on B.Cu"):
        rows = classify_unrouted([entry(n, first.format(n)) for n in ("N3", "N1", "N2")], finished, "converged")
        assert [(r["net"], r["cause"]) for r in rows] == [
            ("N1", "via_blocked"), ("N2", "escape_blocked"), ("N3", "path_blocked")]
        assert rows[1]["unescaped_pads"] == ["U1-1"]
        assert all(r["run_stop"] == "converged" and r["stage"] == "S6c" for r in rows)


def test_keepouts_skip_pad_overlap_and_net_copper_with_reason():
    from eda_agent.placer.route.check import keepouts_from

    square = [[0, 0], [1, 0], [1, 1]]
    rows = [
        {"uuid": "a" * 36, "kind": "mask_aperture", "source": "board", "copper": "F.Cu",
         "outlines": [square], "overlapping_pads": [], "net": None},
        {"uuid": "b" * 36, "kind": "mask_aperture", "source": "footprint:JP1", "copper": "B.Cu",
         "outlines": [square], "overlapping_pads": ["JP1-1"], "net": None},
        {"uuid": "c" * 36, "kind": "copper_graphic", "source": "board", "copper": "F.Cu",
         "outlines": [square], "overlapping_pads": [], "net": "GND"},
    ]
    keep, skipped = keepouts_from(rows)
    assert [k.name for k in keep] == ["mask_aperture_aaaaaaaa_0"]
    assert [s["reason"] for s in skipped] == ["overlaps pads JP1-1", "carries net GND"]


def test_dsn_layer_type_and_wire_keepout():
    text = DSN.replace("(layer B.Cu (type signal))", "(layer In1.Cu (type signal))\n    (layer B.Cu (type signal))")
    out = dsn.set_layer_types(text, {"In1.Cu": "power"})
    assert "(layer In1.Cu (type power))" in out and "(layer F.Cu (type signal))" in out
    k = dsn.add_wire_keepouts(DSN, [dsn.Keepout("mz", "F.Cu", ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0)))])
    assert '(wire_keepout "mz" (polygon F.Cu 0' in k and "(keepout " not in k


def test_power_tap_reaches_an_inner_plane_on_a_four_layer_board():
    outline = [(0, 0), (10, 0), (10, 10), (0, 10)]
    pad = Pad("1", "GND", (0.5, 0), (0.4, 0.3), layers=("F.Cu",))
    chip = Footprint("U1", "", "", 4, 4, 0, side="F", pads=[pad])
    layers = [CopperLayer(n, n, i) for i, n in enumerate(("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))]
    board = Board("power4", [chip], outline=[Outline(outline)], copper_layers=layers,
                  nets=[Net("GND", "Default", 1)], netclasses=[NetClass("Default", 0.15, 0.2, 0.5, 0.3)],
                  zones=[{"net": "GND", "layers": ["In1.Cu"], "outline": [outline]}], rules={"hole_to_hole": 0.25})

    track, via = plan_power_tap(board, chip, pad, "In1.Cu", fixed=[])
    assert track["layer"] == "F.Cu" and via["layers"] == ["F.Cu", "B.Cu"]
    blocker = {"net": "OTHER", "layer": "In2.Cu", "width": 0.2,
               "points": [(via["x"], 2), (via["x"], 6)]}
    alternative = plan_power_tap(board, chip, pad, "In1.Cu", fixed=[blocker])
    assert alternative is not None
    assert abs(alternative[1]["x"] - via["x"]) >= 0.5 - 1e-6
    assert plan_power_tap(board, chip, pad, "F.Cu", fixed=[]) is None  # the pad's own layer is not a tap target


def test_s6a_selects_links_and_taps_from_the_intent_only(monkeypatch):
    from eda_agent.placer.route import s6a

    outline = [(0, 0), (20, 0), (20, 20), (0, 20)]
    vdd = Pad("1", "+3V3", (-1.0, 0), (0.4, 0.3), layers=("F.Cu",))
    gnd = Pad("2", "GND", (1.0, 0), (0.4, 0.3), layers=("F.Cu",))
    chip = Footprint("U1", "", "", 8, 10, 0, side="F", pads=[vdd, gnd])
    cap = Footprint("C1", "", "", 5, 10, 0, side="F",
                    pads=[Pad("1", "+3V3", (0.5, 0), (0.4, 0.4), layers=("F.Cu",)),
                          Pad("2", "GND", (-0.5, 0), (0.4, 0.4), layers=("F.Cu",))])
    other = Footprint("C2", "", "", 5, 14, 0, side="F",
                      pads=[Pad("1", "+3V3", (0.5, 0), (0.4, 0.4), layers=("F.Cu",)),
                            Pad("2", "GND", (-0.5, 0), (0.4, 0.4), layers=("F.Cu",))])
    board = Board("s6a", [chip, cap, other], outline=[Outline(outline)],
                  copper_layers=[CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("B.Cu", "B.Cu", 1)],
                  nets=[Net("GND", "Default", 3), Net("+3V3", "Default", 3)],
                  netclasses=[NetClass("Default", 0.15, 0.2, 0.5, 0.3)],
                  zones=[{"net": "GND", "layers": ["B.Cu"], "outline": [outline]}], rules={"hole_to_hole": 0.25})

    def claim(value, confidence=0.9):
        return {"value": value, "source": {"kind": "motif"}, "confidence": confidence, "confirmed_by_user": False}

    intent = {"parts": {
        "U1": {"role": claim("ic")},
        "C1": {"role": claim("decap"), "target": claim({"ref": "U1", "net": "+3V3", "pins": ["1"]})},
        "C2": {"role": claim("decap"), "target": claim({"ref": None, "net": "+3V3", "candidates": ["U1"]}, 0.3)},
    }, "diff_pairs": [claim({"positive": "USB+", "negative": "USB-"})]}
    reserved = []

    def route_pairs(_board, pairs, *, fixed, retry_coarse):
        assert pairs == [("USB+", "USB-")]
        assert retry_coarse is False
        reserved.extend(fixed)
        return {"copper": [], "rows": [], "rounds": []}

    monkeypatch.setattr(s6a, "_route_pairs", route_pairs)
    planned = s6a.plan(board, intent, power_first=True)
    report = planned["report"]
    assert [(r["cap"], r["routed"]) for r in report["links"]] == [("C1", True)]  # C2's target is not settled
    assert [(r["ref"], r["pad"], r["routed"]) for r in report["taps"]] == [("U1", "2", True)]  # only U1 is active
    tracks = s6a._segments([c for c in planned["copper"] if "points" in c])
    assert s6a.TAP_MAX_MM == 2.0 and tracks and all(len(t["points"]) == 2 for t in tracks)
    assert {item["net"] for item in reserved} == {"+3V3", "GND"}
    assert any("drill" in item for item in reserved)
    reserved.clear()
    s6a.plan(board, intent)
    assert not reserved


def test_s6a_rejects_baseline_with_different_board_or_timeout(tmp_path, monkeypatch):
    import json
    from dataclasses import asdict

    from eda_agent.placer.route import s6a
    from eda_agent.placer.toolchain import sha256_file

    bare = tmp_path / "bare.kicad_pcb"
    bare.write_text("(pcb test)", encoding="utf-8")
    config = RouterConfig(neck_width_um=150, timeout_s=300)
    tools = {"kicad": {"version": "10"}, "java": {"version": "25"}, "freerouting": {"sha256": "router"}}
    monkeypatch.setattr(s6a.run_report, "tools", lambda: tools)
    baseline = tmp_path / "run_report.json"
    baseline.write_text(json.dumps({
        "kind": "route_check", "passed": True, "tools": tools,
        "inputs": {"board": {"sha256": sha256_file(bare)}},
        "config": {"router": asdict(config), "obstacle_keepouts": True},
    }), encoding="utf-8")
    assert s6a._reuse_baseline(baseline, bare, config)["passed"]
    with pytest.raises(ValueError, match="router settings differ"):
        s6a._reuse_baseline(baseline, bare, RouterConfig(neck_width_um=150, timeout_s=600))
    bare.write_text("(pcb different)", encoding="utf-8")
    with pytest.raises(ValueError, match="board or project rules differ"):
        s6a._reuse_baseline(baseline, bare, config)
