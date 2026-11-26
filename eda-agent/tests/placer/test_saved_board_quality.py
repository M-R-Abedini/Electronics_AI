# SPDX-License-Identifier: MIT
"""Regressions for access, zone readback and per-artifact acceptance."""

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from eda_agent.placer import apply as apply_module
from eda_agent.placer import quality
from eda_agent.placer.apply import ApplyError, resolve_run
from eda_agent.placer.eval import legality, lint
from eda_agent.placer.ir import Board, CopperLayer, Courtyard, Footprint, Outline, Pad
from eda_agent.placer.place import analytical, mechanical, problem, review
from shapely.geometry import GeometryCollection


def _footprint(ref, name, x, y, *, side="F", value="", description=""):
    return Footprint(ref, value, name, x, y, 0, side, pads=[Pad("1", "SIG", (0, 0), (0.2, 0.2))],
                     courtyard=Courtyard("file", [[(-1, -1), (1, -1), (1, 1), (-1, 1)]]),
                     description=description)


def _board(*parts):
    return Board(name="test", footprints=list(parts), outline=[Outline(
        [(0, 0), (30, 0), (30, 20), (0, 20)], holes=[[(10, 3), (16, 3), (16, 9), (10, 9)]]
    )])


def test_access_uses_package_and_baseline_instead_of_reference_prefix():
    board = _board(
        _footprint("H5", "Custom:TYPE-C_16PIN", 2, 2),
        _footprint("H6", "Custom:TYPE-C_16PIN", 2, 5, side="B"),
        _footprint("J2", "Custom:TF-SMD_TF-110", 6.4, 6),
        _footprint("X9", "Custom:HANRUN_HR911105A", 28, 8),
        _footprint("H4", "Connector:FPC_1x22", 6, 18, side="B"),
        _footprint("J11", "Connector_PinHeader:PinHeader_1x06_Vertical", 22, 12),
        _footprint("R1", "Resistor_SMD:R_0402", 6, 3, description="Surface mount with two terminals"),
        _footprint("D1", "OLIMEX_Diodes-FP:SMA-KA", 20, 12),
    )
    rows = {row["ref"]: row for row in mechanical.interface_inventory(board)}
    assert set(problem.fixed_parts(board)) == {"H4", "H5", "H6", "J2", "X9"}
    assert rows["X9"]["kind"] == "ethernet"
    assert rows["J11"]["status"] == "internal"
    assert "R1" not in rows
    assert "D1" not in rows
    assert rows["H6"]["pose"][3] == "B"


def test_mating_envelope_crosses_real_cutout_and_rejects_inward_orientation():
    board = _board(_footprint("H5", "Custom:TYPE-C", 8, 6, side="B"))
    fp = board.fp("H5")
    outline = legality.board_polygon(board)
    access = {"kind": "planar", "entry_mm": [0, 0], "exit_mm": [5, 0], "width_mm": 2}
    assert mechanical._fits_access(fp, access, outline, [], GeometryCollection())
    assert not mechanical._fits_access(replace(fp, rotation=180), access, outline, [], GeometryCollection())
    assert not mechanical._fits_access(fp, access, outline, [mechanical.access_geometry(fp, access)], GeometryCollection())


def test_review_catches_protected_rotation_stale_fill_and_artwork():
    original = _board(_footprint("H5", "Custom:TYPE-C", 2, 2),
                      _footprint("R1", "Resistor_SMD:R_0402", 8, 15))
    original.artwork = [{"uuid": "qrcode", "side": "F", "bounds_mm": [19, 14, 21, 16], "owner": None, "label": "QR"}]
    original.zones = [{"uuid": "ground", "net": "GND", "layers": ["F.Cu"],
                       "filled": True, "filled_area_mm2": {"F.Cu": 30}}]
    candidate = original.copy()
    candidate.fp("H5").rotation = 180
    candidate.fp("R1").x = 20
    reviewer = quality.PlacementReview(original, {}, problem.fixed_parts(original))
    detected = reviewer.compare(candidate)
    assert {row["kind"] for row in detected} >= {"protected_pose_changed", "stale_zone_fill", "artwork_occluded"}
    assert review._repair_actions(detected) == ("restore_protected", "repair_local")
    assert "restore_reference" not in review._repair_actions(detected)
    assert review._repair_actions([quality.finding("stale_zone_fill")]) == ("refresh_draft",)
    fresh = original.copy()
    fresh.zones[0].update(filled=False, filled_area_mm2={"F.Cu": 0})
    fresh.artwork.append({"uuid": "created-leader", "side": "F", "bounds_mm": [7, 14, 9, 16],
                          "owner": None, "label": None})
    assert quality.improves(detected, reviewer.compare(fresh))
    assert not any(row["kind"] == "artwork_occluded" for row in reviewer.compare(fresh))
    assert quality.zone_changes(original, fresh) == []


def test_native_reference_plane_respects_fill_holes_and_unconnected_islands():
    ground = [
        {"outer": [(0, 0), (8, 0), (8, 8), (0, 8)],
         "holes": [[(2, 2), (6, 2), (6, 6), (2, 6)]], "island": False},
        {"outer": [(15, 0), (20, 0), (20, 5), (15, 5)], "holes": [], "island": True},
    ]
    board = _board()
    board.copper_layers = [CopperLayer("Bottom", "B.Cu", 1, "signal")]
    board.zones = [{"net": "GND", "layers": ["Bottom"], "fill_geometry": {"Bottom": ground},
                    "outline": [[(0, 0), (30, 0), (30, 20), (0, 20)]]}]
    planes = lint._build_reference_planes(board, {"Bottom": "B.Cu"}, {"B.Cu": 1}, [], [])
    assert planes["B.Cu"]["GND"].area == 48


def test_sparse_density_matches_scalar_windows():
    board = _board(_footprint("R1", "Resistor:SMD", 8, 10))
    solver = analytical.Analytical(problem.build(board))
    rects = np.array([(-0.1, -0.05, 0.1, 0.05), (6, 5, 12, 8), (35, 20, 36, 24), (29.7, 18, 30.2, 20)])
    parts, cells, area = solver._density_contributions(rects)
    actual = np.zeros((len(rects), solver.my * solver.mx))
    actual[parts, cells] = area
    expected = np.zeros((len(rects), solver.my, solver.mx))
    for index, (x0, y0, x1, y1) in enumerate(rects):
        width, height = x1 - x0, y1 - y0
        ew, eh = max(width, 1.414 * solver.bw), max(height, 1.414 * solver.bw)
        x, y = (x0 + x1) / 2, (y0 + y1) / 2
        if (window := solver._window((x - ew / 2, y - eh / 2, x + ew / 2, y + eh / 2))) is not None:
            ys, xs, overlap = window
            expected[index, ys, xs] = overlap * (width * height) / (ew * eh)
    np.testing.assert_allclose(actual, expected.reshape(actual.shape), atol=1e-12, rtol=0)


def test_all_legacy_zones_are_compared_even_when_they_share_a_nil_uuid():
    board = _board()
    board.zones = [
        {"uuid": "00000000-0000-0000-0000-000000000000", "net": net, "layers": ["F.Cu"], "filled": False}
        for net in ("GND", "POWER")
    ]
    saved = board.copy()
    saved.zones.reverse()
    assert quality.zone_changes(board, saved) == []
    saved.zones[1]["net"] = "ALTERED"
    assert quality.zone_changes(board, saved) == [board.zones[0]["uuid"]]


def test_legacy_artwork_may_be_renumbered_but_cannot_disappear():
    original = _board()
    original.source["upgraded_from_legacy"] = True
    original.artwork = [{"uuid": "read-one", "side": "B", "owner": None, "label": "12V",
                         "bounds_mm": [1, 1, 2, 2]}]
    reviewer = quality.PlacementReview(original, {}, {})
    saved = original.copy()
    saved.artwork[0]["uuid"] = "read-two"
    assert reviewer.compare(saved) == []
    saved.artwork.clear()
    assert reviewer.compare(saved)[0]["kind"] == "authored_artwork_missing"


def _report(tmp_path: Path):
    source, placed, routed = [tmp_path / name for name in ("source.kicad_pcb", "placed.kicad_pcb", "route.kicad_pcb")]
    for path in (source, placed, routed):
        path.write_text(path.stem)
        path.with_suffix(".kicad_dru").write_text("reviewed project rules")
    report = {"inputs": {"board": quality.identity(source), "board_artifact": quality.identity(source)},
              "artifacts": {"placed_board": str(placed), "routed_board": str(routed)},
              "quality": {"placement": {"status": "approved", "artifact": quality.identity(placed)},
                          "routing": {"status": "trial", "artifact": quality.identity(routed)}}}
    path = tmp_path / "run_report.json"
    path.write_text(json.dumps(report))
    return path, placed


def test_timeout_never_approves_routed_file_and_changed_artifact_is_rejected(tmp_path):
    path, placed = _report(tmp_path)
    metric = {"settled": True, "router_stop": "timeout", "unrouted": 151,
              "drc_track_dangling": 0, "drc_via_dangling": 0}
    route = quality.routing_status(metric, [], {"unfilled": []})
    assert route["status"] == "trial"
    assert route["reasons"] == ["router_unsettled", "open_connections"]
    assert quality.routing_status({**metric, "unrouted": 0, "router_stop": "completed"}, [],
                                  {"unfilled": ["zone-a"]})["status"] == "failed"
    with pytest.raises(ApplyError, match="routed output has no current approval"):
        resolve_run(path)
    assert resolve_run(path, use_routed=False)["artifact_path"] == placed
    placed.with_suffix(".kicad_dru").write_text("mutated")
    with pytest.raises(ApplyError, match="project rules changed"):
        resolve_run(path, use_routed=False)


def test_apply_rolls_back_board_if_publication_of_reviewed_rules_fails(tmp_path, monkeypatch):
    path, placed = _report(tmp_path)
    target = tmp_path / "out.kicad_pcb"
    copy = apply_module._atomic_copy

    def failing_copy(src, dst, **options):
        if src == placed.with_suffix(".kicad_dru"):
            raise ApplyError("rule changed during copy")
        return copy(src, dst, **options)

    monkeypatch.setattr(apply_module, "_atomic_copy", failing_copy)
    with pytest.raises(ApplyError, match="rule changed during copy"):
        apply_module.apply_run(path, use_routed=False, target_path=target, history_dir=tmp_path / "history")
    assert not target.exists()


def test_drc_attribute_honors_position_invariant_exclusions_and_same_fp_pads(tmp_path):
    from eda_agent.placer.route import drc

    board_path = tmp_path / "demo.kicad_pcb"
    board_path.write_text("", encoding="utf-8")
    pro = {
        "board": {
            "design_settings": {
                "drc_exclusions": [
                    ["solder_mask_bridge|1000|2000|uuid-a|uuid-b", ""],
                ]
            }
        }
    }
    board_path.with_suffix(".kicad_pro").write_text(json.dumps(pro), encoding="utf-8")
    exclusions = drc.load_exclusions(board_path)
    assert ("solder_mask_bridge", frozenset({"uuid-a", "uuid-b"})) in exclusions

    routed = {
        "violations": [
            {
                "type": "solder_mask_bridge",
                "severity": "error",
                "items": [
                    {"uuid": "uuid-a", "description": "Pad 1 [GND] of U3 on F.Cu"},
                    {"uuid": "uuid-b", "description": "Pad 48 [VCC] of U3 on F.Cu"},
                ],
            },
            {
                "type": "solder_mask_bridge",
                "severity": "error",
                "items": [
                    {"uuid": "uuid-c", "description": "Pad 37 [N1] of U3 on F.Cu"},
                    {"uuid": "uuid-d", "description": "Pad 38 [N2] of U3 on F.Cu"},
                ],
            },
            {
                "type": "solder_mask_bridge",
                "severity": "error",
                "items": [
                    {"uuid": "uuid-e", "description": "Pad 1 [N1] of C1 on F.Cu"},
                    {"uuid": "uuid-f", "description": "Pad 2 [N2] of R1 on F.Cu"},
                ],
            },
        ]
    }
    attr = drc.attribute(routed, {"violations": []}, exclusions=exclusions)
    assert len(attr.pre_existing) == 2
    assert len(attr.new_other) == 1


def test_side_obstacle_rects_inflates_barcodes_and_respects_sides():
    board = _board(_footprint("R1", "Resistor_SMD:R_0402", 8, 15))
    board.board_area_mm2 = 600.0
    board.artwork = [
        {"uuid": "bc1", "side": "F", "owner": None, "kind": "barcode", "bounds_mm": [10.0, 10.0, 14.0, 14.0], "label": "QR"},
        {"uuid": "txt1", "side": "B", "owner": None, "kind": "text", "bounds_mm": [2.0, 2.0, 5.0, 4.0], "label": "REV1"},
    ]
    obs = problem.side_obstacle_rects(board, set())
    assert len(obs) == 2
    bc = next(o for o in obs if o[4] == 0)
    assert bc[0] == pytest.approx(8.95)
    assert bc[2] == pytest.approx(15.05)
    txt = next(o for o in obs if o[4] == 1)
    assert txt[0] == pytest.approx(1.95)
    assert txt[4] == 1
