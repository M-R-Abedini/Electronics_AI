# SPDX-License-Identifier: MIT
"""Unit and integration tests for IPC-7351 Silkscreen & Assembly Legend Engine."""

from __future__ import annotations

from pathlib import Path

import pytest
from eda_agent.placer import corpus, silkscreen
from eda_agent.placer.io.kicad import read_board, write_board
from eda_agent.placer.ir import (
    Board,
    Capabilities,
    CopperLayer,
    Courtyard,
    Footprint,
    NetClass,
    Outline,
    Pad,
    Stackup,
    derive,
)
from eda_agent.placer.mcp import PlacerService
from eda_agent.placer.place.pipeline import place_board
from eda_agent.placer.route import drc

_STICKHUB_PATH = corpus.get("stickhub").path
_requires_stickhub = pytest.mark.skipif(
    not _STICKHUB_PATH.is_file(),
    reason="StickHub corpus board unavailable (KiCad 10 demos not installed; "
    "set ELECTRONICS_AI_KICAD to the KiCad install root)",
)


def _make_board(gap_mm: float = 2.0) -> Board:
    sq = [(-0.8, -0.5), (0.8, -0.5), (0.8, 0.5), (-0.8, 0.5)]

    def fp(ref: str, x: float, y: float, lib: str = "Resistor_SMD:R_0603", val: str = "10k") -> Footprint:
        pads = [
            Pad("1", "N1", (-0.6, 0.0), (0.5, 0.5), layers=("F.Cu",)),
            Pad("2", "N2", (0.6, 0.0), (0.5, 0.5), layers=("F.Cu",)),
        ]
        return Footprint(
            ref=ref, value=val, lib_id=lib, x=x, y=y, rotation=0.0, side="F",
            pads=pads, courtyard=Courtyard("file", [sq]), uuid=f"fp-{ref}",
        )

    step = 1.6 + gap_mm
    fps = [
        fp("U1", 5.0, 5.0, lib="Package_QFP:LQFP-32", val="MCU"),
        fp("J1", 5.0 + step, 5.0, lib="Connector_USB:USB_C", val="USB_C"),
        fp("D1", 5.0 + 2 * step, 5.0, lib="LED_SMD:LED_0603", val="LED_RED"),
        fp("C1", 5.0, 5.0 + step, lib="Capacitor_SMD:C_0603", val="100nF"),
        fp("C2", 5.0 + step, 5.0 + step, lib="Capacitor_SMD:CP_Elec_6.3x5.4", val="100uF"),
        fp("R1", 5.0 + 2 * step, 5.0 + step, lib="Resistor_SMD:R_0603", val="10k"),
    ]
    b = Board(
        name="silk_unit_board",
        footprints=fps,
        outline=[Outline([(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0)])],
        outline_valid=True,
        board_area_mm2=1200.0,
        copper_layers=[CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("B.Cu", "B.Cu", 1)],
        rules={"min_silk_text_height": 0.5, "min_silk_text_thickness": 0.1, "silk_clearance": 0.1},
        netclasses=[NetClass("Default", 0.2, 0.2, 0.6, 0.3)],
        stackup=Stackup(source="missing"),
        capabilities=Capabilities(can_flip=True, can_rotate_any_angle=True, can_write_copper=True, can_live_apply=False),
    )
    return derive(b)


def test_ipc7351_stroke_and_role_classification() -> None:
    # Ideal 1:6..1:8 ratio and >= 0.15 mm floor
    assert 0.15 <= silkscreen.recommended_stroke_mm(1.0) <= 0.18
    assert 0.15 <= silkscreen.recommended_stroke_mm(1.27) <= 0.18
    assert silkscreen.recommended_stroke_mm(0.80) == 0.15
    # High-precision LPI floor >= 0.12 mm and never < 1:10
    assert silkscreen.recommended_stroke_mm(0.80, min_stroke_mm=0.12) == 0.12
    assert silkscreen.recommended_stroke_mm(1.50, min_stroke_mm=0.12) >= 0.15

    board = _make_board(gap_mm=2.5)
    intent = {
        "parts": {
            "U1": {"role": {"value": "ic"}},
            "J1": {"role": {"value": "connector"}},
            "D1": {"role": {"value": "led"}},
            "C1": {"role": {"value": "decap"}},
            "C2": {"role": {"value": "bulk_cap"}},
            "R1": {"role": {"value": "pull_up"}},
        }
    }
    cfg = silkscreen.build_config(board, intent, policy="auto", fab_profile="ipc_standard")
    assert cfg["density"]["classification"] == "standard"
    assert set(cfg["critical_refs"]) == {"U1", "J1", "D1", "C2"}
    assert set(cfg["non_critical_refs"]) == {"C1", "R1"}
    assert cfg["min_height_mm"] == 0.80
    assert cfg["target_height_mm"] == 1.00
    assert cfg["min_stroke_mm"] == 0.15
    assert cfg["min_pad_clearance_mm"] == 0.20

    # Tight spacing (< 1.0 mm) triggers dense classification automatically
    dense_board = _make_board(gap_mm=0.4)
    dense_cfg = silkscreen.build_config(dense_board, intent, policy="auto")
    assert dense_cfg["density"]["classification"] == "dense"
    assert dense_cfg["effective_mode"] == "dense"


def test_silkscreen_audit_catches_height_stroke_ratio_pad_and_body_issues() -> None:
    texts = [
        # Compliant IPC standard reference
        {
            "uuid": "t1", "ref": "U1", "kind": "field", "text": "U1",
            "layer": "F.Silkscreen", "visible": True,
            "height_mm": 1.0, "width_mm": 1.0, "stroke_mm": 0.15,
            "pad_clearance_mm": 0.35, "under_body": False, "overlaps_silk": False,
        },
        # Compliant condensed reference (0.78 x 1.0 mm)
        {
            "uuid": "t2", "ref": "R1", "kind": "field", "text": "R1",
            "layer": "F.Silkscreen", "visible": True,
            "height_mm": 1.0, "width_mm": 0.78, "stroke_mm": 0.15,
            "pad_clearance_mm": 0.25, "under_body": False, "overlaps_silk": False,
        },
        # Too tiny height + unprintable hairline stroke (< 1:10 ratio) + overlapping pad + under body
        {
            "uuid": "t3", "ref": "C1", "kind": "field", "text": "C1",
            "layer": "F.Silkscreen", "visible": True,
            "height_mm": 0.60, "width_mm": 0.60, "stroke_mm": 0.05,
            "pad_clearance_mm": 0.0, "under_body": True, "overlaps_silk": True,
        },
        # Assembly layer reference (counted separately, never penalized as silkscreen error)
        {
            "uuid": "t4", "ref": "R2", "kind": "field", "text": "R2",
            "layer": "F.Fab", "visible": True,
            "height_mm": 0.50, "width_mm": 0.45, "stroke_mm": 0.08,
            "pad_clearance_mm": None, "under_body": False, "overlaps_silk": False,
        },
    ]
    aud = silkscreen.audit_texts(texts, fab_profile="ipc_standard")
    assert aud["status"] == "fail"
    assert aud["visible_silk_texts"] == 3
    assert aud["visible_fab_texts"] == 1
    assert aud["condensed_count"] == 1
    assert aud["counts"]["below_min_height"] == 1
    assert aud["counts"]["below_min_stroke"] == 1
    assert aud["counts"]["below_min_ratio"] == 1
    assert aud["counts"]["pad_clearance_violations"] == 1
    assert aud["counts"]["under_body"] == 1
    assert aud["counts"]["overlaps"] == 1
    assert aud["cured_ink_spec_um"]["recommended_min_um"] == 15
    assert aud["cured_ink_spec_um"]["recommended_max_um"] == 25


def test_invalid_silkscreen_policy_and_fab_profile_rejected() -> None:
    board = _make_board()
    with pytest.raises(ValueError, match="unsupported silkscreen_policy"):
        silkscreen.build_config(board, policy="bad_policy")
    with pytest.raises(ValueError, match="unsupported fab_profile"):
        silkscreen.build_config(board, fab_profile="unknown_fab")


@_requires_stickhub
def test_silkscreen_policies_end_to_end_on_stickhub(tmp_path: Path) -> None:
    src = _STICKHUB_PATH
    b0 = read_board(src)
    aud0 = silkscreen.audit_board(b0)
    assert aud0["counts"]["errors"] > 0  # StickHub input has tiny 0.4 mm / 0.1 mm overlapping silkscreen

    drc_ref = drc.run(src, tmp_path / "drc_ref.json")

    # 1. Auto IPC-7351 policy dramatically improves DFM score and adds zero KiCad DRC errors
    cfg_auto = silkscreen.build_config(b0, policy="auto", fab_profile="ipc_standard")
    dst_auto = tmp_path / "stickhub_auto.kicad_pcb"
    stats_auto: dict = {}
    write_board(src, b0, dst_auto, silkscreen_config=cfg_auto, result_out=stats_auto)
    aud_auto = silkscreen.audit_board(read_board(dst_auto))
    assert aud_auto["dfm_score"] > aud0["dfm_score"] + 40.0
    assert aud_auto["reference_designators"]["errors"] == 0
    attr_auto = drc.attribute(drc.run(dst_auto, tmp_path / "drc_auto.json"), drc_ref)
    assert attr_auto.counts()["new_non_routing"] == 0

    # 2. Assembly-only policy offloads all reference designators onto F.Fab/B.Fab cleanly
    cfg_asm = silkscreen.build_config(b0, policy="assembly_only")
    dst_asm = tmp_path / "stickhub_asm.kicad_pcb"
    stats_asm: dict = {}
    write_board(src, b0, dst_asm, silkscreen_config=cfg_asm, result_out=stats_asm)
    aud_asm = silkscreen.audit_board(read_board(dst_asm))
    assert stats_asm["silkscreen"]["offloaded_to_fab"] >= 30
    assert stats_asm["silkscreen"]["fab_refs_created"] >= 30
    assert aud_asm["visible_ref_designators"] == 0
    assert aud_asm["visible_fab_texts"] >= len(b0.footprints) - 5
    attr_asm = drc.attribute(drc.run(dst_asm, tmp_path / "drc_asm.json"), drc_ref)
    assert attr_asm.counts()["new_non_routing"] == 0

    # 3. MCP inspect + place_cluster expose silkscreen audit and policy options cleanly
    svc = PlacerService(data_dir=tmp_path / "mcp_runs")
    insp = svc.inspect(str(src))
    assert "silkscreen" in insp and insp["silkscreen"]["visible_silk_texts"] > 0
    res_cl = svc.place_cluster(
        str(src), ["R1", "R2", "C1"], seed=0,
        preserve_routing="outside_cluster", silkscreen_policy="dense", fab_profile="jlcpcb",
    )
    assert res_cl["silkscreen"]["policy"] == "dense"
    assert res_cl["silkscreen"]["fab_profile"] == "jlcpcb"
    assert res_cl["silkscreen"]["offloaded_to_fab"] >= 10
    with pytest.raises(ValueError, match="unsupported silkscreen_policy"):
        place_board(src, out_dir=tmp_path / "bad_pol", route=False, silkscreen_policy="invalid")


def _make_miniature_board() -> Board:
    sq = [(-0.8, -0.8), (0.8, -0.8), (0.8, 0.8), (-0.8, 0.8)]
    tiny = [(-0.3, -0.15), (0.3, -0.15), (0.3, 0.15), (-0.3, 0.15)]
    fps = [
        Footprint(
            ref="U3", value="MS583702BA01-50", lib_id="Sensor_Pressure:MS5837",
            x=6.0, y=6.0, rotation=0.0, side="F",
            pads=[Pad("1", "VDD", (-0.5, 0.0), (0.4, 0.4), layers=("F.Cu",))],
            courtyard=Courtyard("file", [sq]), uuid="fp-U3",
        ),
        Footprint(
            ref="C5", value="470nF_6.3V", lib_id="Capacitor_SMD:C_0201",
            x=3.5, y=6.5, rotation=0.0, side="F",
            pads=[Pad("1", "VDD", (-0.2, 0.0), (0.2, 0.2), layers=("F.Cu",))],
            courtyard=Courtyard("file", [tiny]), uuid="fp-C5",
        ),
        Footprint(
            ref="J1", value="BM23PF0.8-10DP", lib_id="Connector:BM23",
            x=6.0, y=2.0, rotation=0.0, side="B",
            pads=[Pad("1", "GND", (0.0, 0.0), (0.4, 0.4), layers=("B.Cu",))],
            courtyard=Courtyard("file", [sq]), uuid="fp-J1",
        ),
    ]
    b = Board(
        name="mini_sensor_pcb",
        footprints=fps,
        outline=[Outline([(0.0, 0.0), (10.3, 0.0), (10.3, 8.99), (0.0, 8.99)])],
        outline_valid=True,
        board_area_mm2=57.11,
        copper_layers=[CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("B.Cu", "B.Cu", 1)],
        rules={"min_silk_text_height": 0.5, "min_silk_text_thickness": 0.1, "silk_clearance": 0.1},
        netclasses=[NetClass("Default", 0.15, 0.15, 0.4, 0.2)],
        stackup=Stackup(source="missing"),
        capabilities=Capabilities(can_flip=True, can_rotate_any_angle=True, can_write_copper=True, can_live_apply=False),
    )
    return derive(b)


def test_miniature_board_density_sizing_and_fab_overflow_audit() -> None:
    mini = _make_miniature_board()
    dens = silkscreen.estimate_board_density(mini)
    assert dens["is_miniature"] is True
    assert dens["classification"] == "dense"
    assert dens["recommended_height_mm"] == 0.80

    cfg = silkscreen.build_config(mini, policy="auto", fab_profile="ipc_standard")
    assert cfg["is_miniature"] is True
    assert cfg["prefer_dense_size"] is True
    assert cfg["fallback_offload_critical"] is True
    assert cfg["populate_hidden_refs"] is True
    assert cfg["normalize_fab_texts"] is True

    # Blank overlay + giant overflowing F.Fab/B.Fab texts must NEVER score 100 / pass!
    blank_overflow_texts = [
        {
            "uuid": "f1", "ref": "U3", "kind": "graphic_text", "text": "U3",
            "layer": "F.Fab", "visible": True, "height_mm": 0.5, "width_mm": 0.4, "stroke_mm": 0.08,
            "outside_board": False, "pos_mm": [6.0, 6.0], "bbox_mm": [1.0, 0.5],
        },
        {
            "uuid": "f2", "ref": "U3", "kind": "value_field", "text": "MS583702BA01-50",
            "layer": "F.Fab", "visible": True, "height_mm": 1.0, "width_mm": 1.0, "stroke_mm": 0.15,
            "outside_board": True, "pos_mm": [6.0, 8.5], "bbox_mm": [14.96, 1.70],
        },
        {
            "uuid": "f3", "ref": "J1", "kind": "value_field", "text": "BM23PF0.8-10DP",
            "layer": "B.Fab", "visible": True, "height_mm": 1.0, "width_mm": 1.0, "stroke_mm": 0.15,
            "outside_board": True, "pos_mm": [6.0, 2.0], "bbox_mm": [1.70, 21.01],
        },
    ]
    aud_blank = silkscreen.audit_texts(blank_overflow_texts, board=mini)
    assert aud_blank["overlay_mode"] == "assembly_only"
    assert aud_blank["status"] == "warn"
    assert aud_blank["dfm_score"] < 100.0
    assert aud_blank["counts"]["fab_outside_board"] == 2
    assert aud_blank["counts"]["missing_legends"] == 2  # C5 and J1 have no ref text on silk or fab

    # Oversized 1.10 mm overlay text on miniature board triggers warning
    oversized_texts = [
        {
            "uuid": "s1", "ref": "U3", "kind": "field", "text": "U3",
            "layer": "F.Silkscreen", "visible": True, "height_mm": 1.10, "width_mm": 1.10, "stroke_mm": 0.16,
            "pad_clearance_mm": 0.30, "under_body": False, "overlaps_silk": False,
            "outside_board": False, "pos_mm": [8.0, 6.0], "bbox_mm": [2.2, 1.1],
        },
        {
            "uuid": "f4", "ref": "C5", "kind": "graphic_text", "text": "C5",
            "layer": "F.Fab", "visible": True, "height_mm": 0.30, "width_mm": 0.25, "stroke_mm": 0.05,
            "outside_board": False, "pos_mm": [3.5, 6.5], "bbox_mm": [0.5, 0.3],
        },
        {
            "uuid": "f5", "ref": "J1", "kind": "graphic_text", "text": "J1",
            "layer": "B.Fab", "visible": True, "height_mm": 0.40, "width_mm": 0.35, "stroke_mm": 0.06,
            "outside_board": False, "pos_mm": [6.0, 2.0], "bbox_mm": [0.8, 0.4],
        },
    ]
    aud_over = silkscreen.audit_texts(oversized_texts, board=mini)
    assert aud_over["overlay_mode"] == "mixed_overlay_and_fab"
    assert aud_over["counts"]["oversized_for_board"] == 1
    assert any(f["rule"] == "silk_oversized_for_miniature_board" for f in aud_over["findings"])


def test_large_dense_board_does_not_force_populate_hidden_refs() -> None:
    sq = [(-0.8, -0.5), (0.8, -0.5), (0.8, 0.5), (-0.8, 0.5)]
    fps = [
        Footprint(
            ref=f"R{i + 1}", value="10k", lib_id="Resistor_SMD:R_0402",
            x=4.0 + (i % 5) * 4.0, y=4.0 + (i // 5) * 4.0, rotation=0.0, side="F",
            pads=[Pad("1", "N1", (0.0, 0.0), (0.4, 0.4), layers=("F.Cu",))],
            courtyard=Courtyard("file", [sq]), uuid=f"fp-R{i + 1}",
        )
        for i in range(20)
    ]
    b = derive(Board(
        name="large_hidden_board",
        footprints=fps,
        outline=[Outline([(0.0, 0.0), (50.0, 0.0), (50.0, 25.0), (0.0, 25.0)])],
        outline_valid=True,
        board_area_mm2=1250.0,
        copper_layers=[CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("B.Cu", "B.Cu", 1)],
        netclasses=[NetClass("Default", 0.2, 0.2, 0.6, 0.3)],
        stackup=Stackup(source="missing"),
    ))
    cfg = silkscreen.build_config(b, policy="auto")
    assert cfg["is_miniature"] is False
    assert cfg["populate_hidden_refs"] is False


def test_gallery_and_diagnose_surface_silkscreen_and_assembly_visual_qa(tmp_path: Path, monkeypatch) -> None:
    import base64
    import json

    from eda_agent.placer import diagnose as diag_mod
    from eda_agent.placer import mcp as placer_mcp

    mini = _make_miniature_board()
    mini.silkscreen_texts = [
        {
            "uuid": "f1", "ref": "U3", "kind": "graphic_text", "text": "U3",
            "layer": "F.Fab", "visible": True, "height_mm": 0.5, "width_mm": 0.4, "stroke_mm": 0.08,
            "outside_board": False, "pos_mm": [6.0, 6.0], "bbox_mm": [1.0, 0.5],
        },
        {
            "uuid": "f2", "ref": "U3", "kind": "value_field", "text": "MS583702BA01-50",
            "layer": "F.Fab", "visible": True, "height_mm": 1.0, "width_mm": 1.0, "stroke_mm": 0.15,
            "outside_board": True, "pos_mm": [6.0, 8.5], "bbox_mm": [14.96, 1.70],
        },
        {
            "uuid": "f3", "ref": "C5", "kind": "graphic_text", "text": "C5",
            "layer": "F.Fab", "visible": True, "height_mm": 0.35, "width_mm": 0.30, "stroke_mm": 0.06,
            "outside_board": False, "pos_mm": [3.5, 6.5], "bbox_mm": [0.6, 0.35],
        },
        {
            "uuid": "f4", "ref": "J1", "kind": "graphic_text", "text": "J1",
            "layer": "B.Fab", "visible": True, "height_mm": 0.5, "width_mm": 0.4, "stroke_mm": 0.08,
            "outside_board": False, "pos_mm": [6.0, 2.0], "bbox_mm": [1.0, 0.5],
        },
    ]

    run_dir = tmp_path / "run_mini"
    run_dir.mkdir()
    pcb_path = run_dir / "mini.placed.kicad_pcb"
    pcb_path.write_bytes(b"mock")
    rep_path = run_dir / "run_report.json"
    rep_path.write_text(json.dumps({
        "kind": "score",
        "inputs": {"board": {"path": str(pcb_path)}},
        "artifacts": {"placed_board": str(pcb_path), "run_dir": str(run_dir)},
        "metrics": {},
        "stages": {},
        "failures": [],
    }), encoding="utf-8")

    monkeypatch.setattr(placer_mcp, "read_board", lambda _: mini)
    monkeypatch.setattr(placer_mcp, "review_images", lambda _: [("top", b"TOP"), ("bottom", b"BOT")])
    monkeypatch.setattr(diag_mod, "read_board", lambda _: mini)

    svc = PlacerService(data_dir=tmp_path / "svc")
    gal = svc.gallery([str(rep_path)], include_images=True)
    qa = gal.structuredContent["silkscreen_visual_qa"][0]
    assert qa["overlay_mode"] == "assembly_only"
    assert qa["is_miniature"] is True
    assert qa["fab_outside_board"] == 1
    assert qa["dfm_score"] < 100.0
    assert [v["side"] for v in gal.structuredContent["views"]] == ["top", "bottom", "assembly_fab"]
    fab_img = [item for item in gal.content if item.type == "image"][-1]
    assert base64.b64decode(fab_img.data).startswith(b"\x89PNG\r\n\x1a\n")

    diag = svc.diagnose(str(rep_path), include_images=True)
    assert diag.structuredContent["silkscreen"]["counts"]["fab_outside_board"] == 1
    assert diag.structuredContent["finding_counts"]["fab_text_outside_board"] == 1
    diag_imgs = [item for item in diag.content if item.type == "image"]
    assert diag_imgs and base64.b64decode(diag_imgs[0].data).startswith(b"\x89PNG\r\n\x1a\n")


def test_audit_catches_overlapping_oversized_fab_and_mirror_mismatches() -> None:
    mini = _make_miniature_board()
    messy_fab = [
        {
            "uuid": "f1", "ref": "U3", "kind": "value_field", "text": "MS583702BA01-50",
            "layer": "F.Fab", "visible": True, "height_mm": 0.8, "width_mm": 0.8, "stroke_mm": 0.12,
            "outside_board": False, "overlaps_fab": True, "exceeds_body": True, "mirror_mismatch": True,
            "pos_mm": [6.0, 6.0], "bbox_mm": [7.5, 1.3],
        },
        {
            "uuid": "f2", "ref": "C5", "kind": "graphic_text", "text": "C5",
            "layer": "F.Fab", "visible": True, "height_mm": 0.8, "width_mm": 0.8, "stroke_mm": 0.12,
            "outside_board": False, "overlaps_fab": True, "exceeds_body": True, "mirror_mismatch": False,
            "pos_mm": [3.5, 6.5], "bbox_mm": [2.2, 1.3],
        },
        {
            "uuid": "f3", "ref": "J1", "kind": "graphic_text", "text": "J1",
            "layer": "B.Fab", "visible": True, "height_mm": 0.5, "width_mm": 0.4, "stroke_mm": 0.08,
            "outside_board": False, "overlaps_fab": False, "exceeds_body": False, "mirror_mismatch": True,
            "pos_mm": [6.0, 2.0], "bbox_mm": [1.0, 0.5],
        },
    ]
    aud = silkscreen.audit_texts(messy_fab, board=mini)
    assert aud["status"] == "warn"
    assert aud["dfm_score"] < 75.0
    assert aud["counts"]["fab_overlaps"] == 2
    assert aud["counts"]["fab_oversized"] == 2
    assert aud["counts"]["mirror_mismatches"] == 2
    rules_hit = {f["rule"] for f in aud["findings"]}
    assert {"fab_text_overlap", "fab_text_oversized_for_body", "text_mirror_mismatch"} <= rules_hit
