# SPDX-License-Identifier: MIT
"""Unit tests of the macro contract's transform algebra (no KiCad; KiCad read-back is the F0c proof)."""

from __future__ import annotations

import math

from eda_agent.placer.ir import Footprint, Pad
from eda_agent.placer.macro import (
    Macro,
    MacroPart,
    MTrack,
    MVia,
    MZone,
    Port,
    Transform,
    flip_layer,
    instantiate,
    predicted_pads,
    to_local,
    to_world,
)


def _macro():
    return Macro(
        template="t", home_side="F", layer_count=4,
        parts=[MacroPart("J1", "u1", 1.0, 0.0, 90.0, "same"), MacroPart("C1", "u2", -1.0, 0.5, 0.0, "opposite")],
        tracks=[MTrack("F.Cu", 0.2, "A", [(0.0, 0.0), (1.0, 0.0)]), MTrack("In1.Cu", 0.2, "B", [(0.0, 1.0), (0.5, 1.5)])],
        vias=[MVia(0.5, 0.0, 0.6, 0.3, "A", ("F.Cu", "B.Cu"))],
        zones=[MZone("A", ["F.Cu"], [[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]])],
        ports=[Port("A", "pad", "F.Cu", 1.0, 0.0, 0.0, 0.5)],
    )


def test_flip_layer_maps_outer_and_inner_layers():
    assert flip_layer("F.Cu", 4) == "B.Cu" and flip_layer("B.Cu", 4) == "F.Cu"
    assert flip_layer("In1.Cu", 4) == "In2.Cu" and flip_layer("In2.Cu", 4) == "In1.Cu"
    assert flip_layer("In3.Cu", 8) == "In4.Cu"


def test_world_local_round_trip_with_and_without_mirror():
    t = Transform(10.0, 20.0, 90.0, "B")
    for mirror in (False, True):
        for p in ((1.0, 0.0), (0.3, -2.5), (-4.0, 1.25)):
            back = to_local(to_world(p, t, mirror), t, mirror)
            assert math.isclose(back[0], p[0], abs_tol=1e-6) and math.isclose(back[1], p[1], abs_tol=1e-6)


def test_instantiate_home_side_is_rigid_and_other_side_mirrors():
    m = _macro()
    same = instantiate(m, Transform(10.0, 10.0, 90.0, "F"))
    assert not same.mirror
    assert same.poses["u1"] == {"ref": "J1", "x": 10.0, "y": 9.0, "rotation": 180.0, "side": "F"}
    assert same.poses["u2"]["side"] == "B"
    flipped = instantiate(m, Transform(10.0, 10.0, 0.0, "B"))
    assert flipped.mirror
    assert flipped.poses["u1"]["x"] == 9.0 and flipped.poses["u1"]["rotation"] == 90.0  # 180 - 90
    assert flipped.poses["u1"]["side"] == "B" and flipped.poses["u2"]["side"] == "F"
    assert [t.layer for t in flipped.tracks] == ["B.Cu", "In2.Cu"]
    assert flipped.vias[0].layers == ("B.Cu", "F.Cu") and flipped.zones[0].layers == ["B.Cu"]
    assert flipped.ports[0].escape_dir == 180.0


def test_predicted_pads_follow_kicad_flip_convention():
    fp = Footprint("R1", "v", "lib:x", 0.0, 0.0, 0.0, "F",
                   pads=[Pad("1", "A", (-0.5, 0.2), (0.4, 0.4)), Pad("2", "B", (0.5, 0.2), (0.4, 0.4))])
    same = predicted_pads(fp, {"x": 1.0, "y": 2.0, "rotation": 0.0, "side": "F"})
    assert same == [("1", (0.5, 2.2)), ("2", (1.5, 2.2))]
    # left-right flip about the footprint origin: x mirrors in board coordinates
    flipped = predicted_pads(fp, {"x": 0.0, "y": 0.0, "rotation": 180.0, "side": "B"})
    assert flipped == [("1", (0.5, 0.2)), ("2", (-0.5, 0.2))]
