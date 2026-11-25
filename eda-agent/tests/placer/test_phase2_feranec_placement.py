# SPDX-License-Identifier: MIT
"""Phase 2 Feranec placement unit tests: cluster SA moves, repulsion, rigid sub_rects, balanced decaps, S2b."""

from __future__ import annotations

from eda_agent.placer.intent import infer, model
from eda_agent.placer.ir import (
    Board,
    CopperLayer,
    Courtyard,
    Footprint,
    Net,
    NetClass,
    Outline,
    Pad,
    derive,
)
from eda_agent.placer.place import analytical, blocks, problem, rigid


def _rect_cy(w: float, h: float) -> Courtyard:
    return Courtyard("file", [[(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]])


def _pad(num: str, net: str | None, x: float = 0.0, y: float = 0.0, *, fn: str | None = None,
         ptype: str | None = None) -> Pad:
    return Pad(number=num, net=net, local=(x, y), size=(0.5, 0.5), kind="smd",
               layers=("F.Cu",), function=fn, pin_type=ptype)


def _board(fps: list[Footprint], w: float = 40.0, h: float = 40.0) -> Board:
    seen: dict[str, int] = {}
    for fp in fps:
        for p in fp.pads:
            if p.net:
                seen[p.net] = seen.get(p.net, 0) + 1
    b = Board(
        name="phase2_test",
        footprints=fps,
        outline=[Outline([(0, 0), (w, 0), (w, h), (0, h)])],
        board_area_mm2=w * h,
        copper_layers=[CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("B.Cu", "B.Cu", 1)],
        netclasses=[NetClass("Default", 0.15, 0.15, 0.5, 0.25)],
        nets=[Net(name=n, netclass="Default", pads=c) for n, c in sorted(seen.items())],
        source={"sha256": "0" * 64},
    )
    return derive(b)


def test_esd_inline_betweenness_and_priority_net_weights():
    j1 = Footprint("J1", "USB_C", "Connector_USB:USB_C", 2, 20, 0, locked=True, courtyard=_rect_cy(4, 8), pads=[
        _pad("1", "USB_DP", 2, -1), _pad("2", "USB_DN", 2, 1), _pad("3", "GND", 2, 2),
    ])
    d1 = Footprint("D1", "USBLC6", "Package_TO_SOT_SMD:SOT-23-6", 8, 20, 0, courtyard=_rect_cy(3, 3), pads=[
        _pad("1", "USB_DP", -1, -1), _pad("2", "GND", 0, 0), _pad("3", "USB_DN", -1, 1),
    ])
    u1 = Footprint("U1", "MCU", "Package_QFP:LQFP-48", 25, 20, 0, courtyard=_rect_cy(8, 8), pads=[
        _pad("1", "USB_DP", -4, -1, fn="USB_DP"), _pad("2", "USB_DN", -4, 1, fn="USB_DM"),
        _pad("3", "GND", 0, 0, fn="GND", ptype="power_in"),
    ])
    b = _board([j1, d1, u1])
    intent = infer.draft(b)
    pb = problem.build(b, intent=intent, floorplan=True)
    # ESD should have affinity toward BOTH connector J1 and downstream IC U1 (inline betweenness).
    esd_idx = pb.refs.index("D1")
    esd_affs = [group[1:] for group, _w in pb.affinities if pb.pins[group[0]][0] == esd_idx]
    target_parts = {pb.refs[pb.pins[p][0]] for grp in esd_affs for p in grp}
    assert {"J1", "U1"} <= target_parts


def test_rigid_collapse_preserves_hollow_shield_sub_rects_and_cavity_flags():
    shield_cy = Courtyard(
        "derived",
        [[(-6, -6), (6, -6), (6, 6), (-6, 6)]],
        holes=[[[(-4, -4), (-4, 4), (4, 4), (4, -4)]]],
    )
    j12 = Footprint("J12", "SHIELD", "RF_Shielding:Shield_12x12", 20, 20, 0, locked=True,
                    courtyard=shield_cy, uuid="u-j12", pads=[_pad("1", "GND", -5, -5)])
    u1 = Footprint("U1", "IC", "Package_SO:SOIC-8", 10, 10, 0, courtyard=_rect_cy(3, 3), uuid="u-u1", pads=[
        _pad("1", "+3V3", -1, 0, fn="VCC", ptype="power_in"),
        _pad("2", "GND", 1, 0, fn="GND", ptype="power_in"),
    ])
    c1 = Footprint("C1", "100nF", "Capacitor_SMD:C_0402", 14, 10, 0, courtyard=_rect_cy(1.0, 0.6), uuid="u-c1", pads=[
        _pad("1", "+3V3", -0.4, 0), _pad("2", "GND", 0.4, 0),
    ])
    b = _board([j12, u1, c1])
    intent = infer.draft(b)
    pb = problem.build(b, intent=intent, floorplan=True)
    groups: dict[int, list[int]] = {}
    seeded, _, _ = blocks.seed(pb, intent, pb.poses(), groups=groups)
    macros = rigid.compile_macros(pb, seeded, groups)
    assert macros
    collapsed, _retained = rigid.collapse(pb, macros)
    assert len(collapsed.sub_rects) == len(collapsed.parts)
    assert len(collapsed.cavity_eligible) == len(collapsed.parts)
    j_idx = collapsed.refs.index("J12")
    assert len(collapsed.sub_rects[j_idx]) >= 4
    assert collapsed.cavity_eligible[j_idx] is False


def test_rust_cluster_moves_and_fb_sw_repulsion():
    u1 = Footprint("U1", "BUCK", "Package_SO:SOIC-8", 15, 20, 0, courtyard=_rect_cy(4, 4), pads=[
        _pad("1", "SW", 2, 0, fn="SW"), _pad("2", "FB", -2, 0, fn="FB"),
        _pad("3", "VIN", 0, -2, fn="VIN", ptype="power_in"), _pad("4", "GND", 0, 2, fn="GND", ptype="power_in"),
    ])
    l1 = Footprint("L1", "4.7uH", "Inductor_SMD:L_4040", 21, 20, 0, courtyard=_rect_cy(4, 4), pads=[
        _pad("1", "SW", -1.5, 0), _pad("2", "+3V3", 1.5, 0),
    ])
    r_fb = Footprint("R1", "10k", "Resistor_SMD:R_0402", 18, 20, 0, courtyard=_rect_cy(1.0, 0.6), pads=[
        _pad("1", "FB", -0.4, 0), _pad("2", "GND", 0.4, 0),
    ])
    b = _board([u1, l1, r_fb])
    intent = infer.draft(b)
    pb = problem.build(b, intent=intent, floorplan=True)
    assert pb.clusters
    assert pb.repulsions
    core = problem.rust_problem(pb)
    failed = core.legalize(0.2, 30.0, [])
    assert not failed
    stats = core.anneal(5000, 42, 4.0, 0.05)
    assert stats["iterations"] > 0
    assert core.illegal_parts() == []


def test_opt_in_two_sided_policy_and_analytical_determinism():
    fps = [
        Footprint("J1", "USB", "Connector_USB:USB_C", 1.0, 10, 0, locked=True, courtyard=_rect_cy(4, 8),
                  pads=[_pad("1", "S0", 1, 0), _pad("2", "GND", 1, 2)]),
    ]
    for i in range(1, 15):
        fps.append(Footprint(f"R{i}", "10k", "Resistor_SMD:R_0805", 10, 10, 0, courtyard=_rect_cy(4, 4),
                             pads=[_pad("1", f"S{i - 1}", -1, 0), _pad("2", f"S{i}", 1, 0)]))
    b = _board(fps, w=16.0, h=16.0)
    intent = {"parts": {f"R{i}": {"role": model.claim("signal_passive", "motif", 0.8)} for i in range(1, 15)}}
    sides_l2 = problem.assign_sides(b, "level2_passives_back", {"J1": "locked"}, intent=intent)
    assert sides_l2["J1"] == "F"
    assert "B" in sides_l2.values()
    pb = problem.build(b, intent=intent, floorplan=True)
    r1 = analytical.global_place(pb, max_iters=60)
    r2 = analytical.global_place(pb, max_iters=60)
    assert r1.poses == r2.poses
