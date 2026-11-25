# SPDX-License-Identifier: MIT
"""Step 1 unit tests: safe 2.5D hollow shields with cavity eligibility guard, stepped blocks, opposite-side decap."""

from __future__ import annotations

import math

from eda_agent.placer.eval import legality
from eda_agent.placer.intent import model
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
from eda_agent.placer.place import blocks, decap, problem


def _outline(w: float = 30.0, h: float = 24.0) -> list[Outline]:
    return [Outline([(0.0, 0.0), (w, 0.0), (w, h), (0.0, h)])]


def _smd(ref: str, x: float, y: float, size: tuple[float, float], nets: tuple[str | None, str | None], *,
         value: str = "100nF", side: str = "F", locked: bool = False) -> Footprint:
    w, h = size
    cy = [[(-w / 2 - 0.1, -h / 2 - 0.1), (w / 2 + 0.1, -h / 2 - 0.1),
           (w / 2 + 0.1, h / 2 + 0.1), (-w / 2 - 0.1, h / 2 + 0.1)]]
    pads = [
        Pad("1", nets[0], (-w / 3, 0.0), (w / 3, h * 0.8), layers=("F.Cu",) if side == "F" else ("B.Cu",)),
        Pad("2", nets[1], (w / 3, 0.0), (w / 3, h * 0.8), layers=("F.Cu",) if side == "F" else ("B.Cu",)),
    ]
    return Footprint(ref, value, "C_0402" if ref.startswith("C") else "R_0402", x, y, 0.0, side, locked=locked,
                     pads=pads, courtyard=Courtyard("file", cy), uuid=f"u-{ref}")


def _hollow_shield(x: float = 15.0, y: float = 12.0) -> Footprint:
    outer = [(-5.5, -5.5), (5.5, -5.5), (5.5, 5.5), (-5.5, 5.5)]
    hole = [(-4.4, -4.4), (-4.4, 4.4), (4.4, 4.4), (4.4, -4.4)]
    pads = [
        Pad("1", "GND", (-5.0, -5.0), (1.0, 1.0), layers=("F.Cu",)),
        Pad("2", "GND", (5.0, -5.0), (1.0, 1.0), layers=("F.Cu",)),
        Pad("3", "GND", (5.0, 5.0), (1.0, 1.0), layers=("F.Cu",)),
        Pad("4", "GND", (-5.0, 5.0), (1.0, 1.0), layers=("F.Cu",)),
    ]
    return Footprint("J12", "SHIELD", "una:una_shield_v2", x, y, 0.0, "F", locked=True,
                     pads=pads, courtyard=Courtyard("derived", [outer], 0.05, holes=[[hole]]), uuid="u-J12")


def _board(fps: list[Footprint]) -> Board:
    layers = [
        CopperLayer("F.Cu", "F.Cu", 0, "signal"),
        CopperLayer("In1.Cu", "In1.Cu", 1, "power"),
        CopperLayer("In2.Cu", "In2.Cu", 2, "power"),
        CopperLayer("B.Cu", "B.Cu", 31, "signal"),
    ]
    counts: dict[str, int] = {}
    for fp in fps:
        for p in fp.pads:
            if p.net:
                counts[p.net] = counts.get(p.net, 0) + 1
    b = Board(
        name="step1_test",
        footprints=fps,
        outline=_outline(),
        copper_layers=layers,
        netclasses=[NetClass("Default", 0.15, 0.15, 0.3, 0.2)],
        nets=[Net(n, "Default", c) for n, c in sorted(counts.items())],
    )
    derive(b)
    return b


def test_hollow_shield_allows_low_profile_smd_and_blocks_walls_and_tall_parts():
    shield = _hollow_shield(15.0, 12.0)
    cap_inside = _smd("C1", 15.0, 12.0, (0.8, 0.5), ("+3V3", "GND"))
    cap_wall = _smd("C2", 20.0, 12.0, (1.0, 0.6), ("+3V3", "GND"))
    tall_conn = Footprint(
        "J2", "HDR", "PinHeader_1x2", 13.0, 12.0, 0.0, "F", locked=False,
        pads=[Pad("1", "+3V3", (-0.5, 0.0), (0.8, 0.8), kind="thru_hole", drill=(0.5, 0.5), layers=("F.Cu", "B.Cu"))],
        courtyard=Courtyard("file", [[(-1.0, -0.8), (1.0, -0.8), (1.0, 0.8), (-1.0, 0.8)]]), uuid="u-J2",
    )
    b = _board([shield, cap_inside, cap_wall, tall_conn])
    findings = legality.check(b, fixed={"J12"})
    pairs = {tuple(f.refs): f.detail for f in findings if f.kind == "courtyard_overlap"}
    assert ("C1", "J12") not in pairs
    assert ("C2", "J12") in pairs
    assert ("J12", "J2") in pairs and "tall_part_inside_shield_cavity" in pairs[("J12", "J2")]

    pb = problem.build(b, side_policy="expert")
    core = problem.rust_problem(pb)
    # C1 (idx 1) is legal; C2 (idx 2, on wall) and J2 (idx 3, tall TH inside cavity) are illegal
    assert sorted(core.illegal_parts()) == [2, 3]


def test_decap_refine_pulls_cap_inside_hollow_shield_around_bga():
    shield = _hollow_shield(15.0, 12.0)
    u1 = Footprint(
        "U1", "MCU", "BGA", 15.0, 12.0, 0.0, "F", locked=True,
        pads=[Pad("1", "+3V3", (-1.2, 0.0), (0.3, 0.3), layers=("F.Cu",)),
              Pad("2", "GND", (1.2, 0.0), (0.3, 0.3), layers=("F.Cu",))],
        courtyard=Courtyard("file", [[(-1.8, -1.8), (1.8, -1.8), (1.8, 1.8), (-1.8, 1.8)]]), uuid="u-U1",
    )
    # Cap starts outside the shield 9 mm away from U1
    c1 = _smd("C1", 5.0, 12.0, (0.8, 0.5), ("+3V3", "GND"))
    b = _board([shield, u1, c1])
    intent = {
        "schema": model.SCHEMA,
        "schema_version": model.SCHEMA_VERSION,
        "parts": {
            "C1": {"role": model.claim("decap", "board", 1.0),
                   "target": model.claim({"ref": "U1", "net": "+3V3"}, "board", 1.0)},
        },
    }
    refined, rep = decap.refine(b, intent, {"J12", "U1"})
    assert "C1" in rep["moved"]
    assert not legality.check(refined, fixed={"J12", "U1"})
    assert math.dist((refined.fp("C1").x, refined.fp("C1").y), (u1.x, u1.y)) < 4.0


def test_blocks_seed_stepped_slots_avoid_stacking():
    u1 = Footprint(
        "U1", "MCU", "QFN", 15.0, 12.0, 0.0, "F", locked=True,
        pads=[Pad("1", "+3V3", (-1.5, 0.0), (0.4, 0.4)), Pad("2", "GND", (-1.5, 1.0), (0.4, 0.4))],
        courtyard=Courtyard("file", [[(-2.0, -2.0), (2.0, -2.0), (2.0, 2.0), (-2.0, 2.0)]]), uuid="u-U1",
    )
    caps = [_smd(f"C{i}", 3.0, 3.0, (1.0, 0.5), ("+3V3", "GND")) for i in range(1, 4)]
    b = _board([u1, *caps])
    intent = {
        "schema": model.SCHEMA,
        "schema_version": model.SCHEMA_VERSION,
        "parts": {
            f"C{i}": {"role": model.claim("decap", "board", 1.0),
                      "target": model.claim({"ref": "U1", "net": "+3V3"}, "board", 1.0)}
            for i in range(1, 4)
        },
        "blocks": [model.claim({"kind": "decap_group", "anchor": "U1", "members": ["C1", "C2", "C3"]}, "board", 1.0)],
    }
    pb = problem.build(b, intent=intent, floorplan=True)
    poses, _, _ = blocks.seed(pb, intent, pb.poses())
    pts = [poses[i][:2] for i in (1, 2, 3)]
    assert min(math.dist(pts[a], pts[b_]) for a in range(3) for b_ in range(a + 1, 3)) > 0.3
