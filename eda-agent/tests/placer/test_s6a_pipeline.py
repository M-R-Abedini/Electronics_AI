# SPDX-License-Identifier: MIT
"""Unit tests for automatic DRC-guarded S6a integration inside place_board."""

from __future__ import annotations

from pathlib import Path

from eda_agent.placer.place import pipeline
from eda_agent.placer.route import s6a


def _fake_plan(copper: list[dict]) -> dict:
    return {
        "copper": copper,
        "report": {
            "pairs": [{"pair": ["D+", "D-"], "routed": True}] if any(c["net"] in ("D+", "D-") for c in copper) else [],
            "links": [{"cap": "C1", "target": "U1", "net": "+3V3", "routed": True}]
            if any(c["net"] == "+3V3" for c in copper)
            else [],
            "taps": [],
        },
        "taps": [],
        "plane_links": [],
    }


def test_apply_to_board_fast_path_when_no_critical_copper(tmp_path: Path, monkeypatch):
    board_file = tmp_path / "simple.kicad_pcb"
    board_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(s6a, "read_board", lambda _p: object())
    monkeypatch.setattr(s6a, "plan", lambda *_a, **_kw: _fake_plan([]))

    called = []
    monkeypatch.setattr(s6a, "apply_instance", lambda *_a, **_kw: called.append("apply"))
    monkeypatch.setattr(s6a.drc, "run", lambda *_a, **_kw: called.append("drc"))

    res = s6a.apply_to_board(board_file, {"parts": {}, "diff_pairs": []}, tmp_path / "s6a")
    assert res["applied"] is False
    assert res["reason"] == "no_critical_copper"
    assert res["s6a_board"] is None
    assert called == []


def test_apply_to_board_prunes_violating_net_and_keeps_clean_copper(tmp_path: Path, monkeypatch):
    board_file = tmp_path / "board.kicad_pcb"
    board_file.write_text("", encoding="utf-8")
    copper = [
        {"layer": "F.Cu", "width": 0.15, "net": "D+", "points": [(0.0, 0.0), (2.0, 0.0)]},
        {"layer": "F.Cu", "width": 0.2, "net": "+3V3", "points": [(5.0, 0.0), (6.0, 0.0)]},
    ]
    monkeypatch.setattr(s6a, "read_board", lambda _p: object())
    monkeypatch.setattr(s6a, "plan", lambda *_a, **_kw: _fake_plan(copper))

    written_nets: list[set[str]] = []

    def fake_apply(_src, inst, out, **_kw):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("pcb", encoding="utf-8")
        written_nets.append({t.net for t in inst.tracks})
        return {}

    drc_calls = {"n": 0}

    def fake_drc(_out, _json):
        drc_calls["n"] += 1
        if drc_calls["n"] == 1:
            return {
                "violations": [
                    {
                        "type": "clearance",
                        "items": [{"description": "Track [D+] on F.Cu, length 2.0000 mm"}],
                    }
                ]
            }
        return {"violations": []}

    monkeypatch.setattr(s6a, "apply_instance", fake_apply)
    monkeypatch.setattr(s6a.drc, "run", fake_drc)

    res = s6a.apply_to_board(board_file, {"parts": {}, "diff_pairs": []}, tmp_path / "s6a")
    assert res["applied"] is True
    assert res["reason"] == "applied"
    assert res["pruned_nets"] == ["D+"]
    assert res["copper"] == {"tracks": 1, "vias": 0}
    assert res["drc_touching_s6a"] == []
    assert written_nets == [{"D+", "+3V3"}, {"+3V3"}]


def test_apply_to_board_falls_back_when_drc_persists(tmp_path: Path, monkeypatch):
    board_file = tmp_path / "board.kicad_pcb"
    board_file.write_text("", encoding="utf-8")
    copper = [{"layer": "F.Cu", "width": 0.2, "net": "+3V3", "points": [(5.0, 0.0), (6.0, 0.0)]}]
    monkeypatch.setattr(s6a, "read_board", lambda _p: object())
    monkeypatch.setattr(s6a, "plan", lambda *_a, **_kw: _fake_plan(copper))
    monkeypatch.setattr(
        s6a,
        "apply_instance",
        lambda _s, _i, out, **_kw: (out.parent.mkdir(parents=True, exist_ok=True), out.write_text("x", encoding="utf-8"))[1],
    )
    monkeypatch.setattr(
        s6a.drc,
        "run",
        lambda *_a, **_kw: {
            "violations": [
                {"type": "clearance", "items": [{"description": "Track [+3V3] on F.Cu, length 1.0000 mm"}]}
            ]
        },
    )

    res = s6a.apply_to_board(board_file, {"parts": {}, "diff_pairs": []}, tmp_path / "s6a", prune_drc=False)
    assert res["applied"] is False
    assert res["reason"] == "drc_touching_s6a"
    assert res["s6a_board"] is None
    assert len(res["drc_touching_s6a"]) == 1


def test_place_board_routes_s6a_board_and_falls_back_cleanly(tmp_path: Path, monkeypatch):
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

    sq = [(-0.8, -0.5), (0.8, -0.5), (0.8, 0.5), (-0.8, 0.5)]

    def fp(ref, x, y, nets, locked=False):
        pads = [Pad(str(i + 1), n, (-0.4 + 0.8 * i, 0.0), (0.4, 0.4), layers=("F.Cu",)) for i, n in enumerate(nets)]
        return Footprint(ref, "v", "lib:x", x, y, 0.0, "F", locked=locked, pads=pads,
                         courtyard=Courtyard("file", [sq]), uuid="u-" + ref)

    board = derive(
        Board(
            name="mini",
            footprints=[
                fp("U1", 10.0, 10.0, ["+3V3", "GND"], locked=True),
                fp("C1", 5.0, 10.0, ["+3V3", "GND"]),
            ],
            outline=[Outline([(0, 0), (20, 0), (20, 20), (0, 20)])],
            board_area_mm2=400.0,
            copper_layers=[CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("B.Cu", "B.Cu", 1)],
            netclasses=[NetClass("Default", 0.2, 0.2, 0.6, 0.3)],
            nets=[Net("+3V3", "Default", 2), Net("GND", "Default", 2)],
        )
    )
    src = tmp_path / "mini.kicad_pcb"
    src.write_text("dummy", encoding="utf-8")
    board.source = {"path": str(src), "sha256": "0" * 64}

    monkeypatch.setattr(pipeline, "read_board", lambda _p: board.copy())

    def fake_write(_src, _placed, dst, **_kw):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("placed", encoding="utf-8")
        return dst

    monkeypatch.setattr(pipeline, "write_board", fake_write)
    monkeypatch.setattr(pipeline, "gallery", lambda _p, out, **_kw: (out.write_text("html", encoding="utf-8"), out)[1])

    routed_inputs: list[Path] = []

    def fake_route_check(bpath, *, out_dir, **_kw):
        routed_inputs.append(Path(bpath))
        rb = Path(out_dir) / "mini.routed.kicad_pcb"
        rb.parent.mkdir(parents=True, exist_ok=True)
        rb.write_text("routed", encoding="utf-8")
        return {
            "passed": True,
            "gates": [],
            "failures": [],
            "artifacts": {"routed_board": str(rb)},
            "metrics": {
                "coverage": 1.0,
                "unrouted": 0,
                "denominator": 2,
                "vias": 0,
                "track_length_mm": 5.0,
                "router_stop": "converged",
                "settled": True,
            },
        }

    monkeypatch.setattr(pipeline, "route_check", fake_route_check)

    # Case 1: S6a applies cleanly -> route_check gets s6a_board
    s6a_out = tmp_path / "run1" / "s6a" / "mini.placed.kicad_pcb"

    def fake_s6a_ok(_bp, _intent, out_dir, **_kw):
        s6a_out.parent.mkdir(parents=True, exist_ok=True)
        s6a_out.write_text("s6a", encoding="utf-8")
        return {
            "applied": True,
            "reason": "applied",
            "s6a_board": str(s6a_out),
            "counts": {"pairs": {"planned": 0, "routed": 0}, "links": {"planned": 1, "routed": 1}, "taps": {"planned": 0, "routed": 0}},
            "copper": {"tracks": 1, "vias": 0},
            "drc_touching_s6a": [],
            "pruned_nets": [],
            "report": {},
            "live_taps": [],
        }

    monkeypatch.setattr(s6a, "apply_to_board", fake_s6a_ok)
    rep1 = pipeline.place_board(src, out_dir=tmp_path / "run1", route=True, s6a=True)
    assert routed_inputs[-1] == s6a_out
    assert rep1["stages"]["s6a"]["applied"] is True
    assert rep1["artifacts"]["s6a_board"] == str(s6a_out)

    # Case 2: S6a falls back due to DRC -> route_check gets bare placed board
    monkeypatch.setattr(
        s6a,
        "apply_to_board",
        lambda *_a, **_kw: {
            "applied": False,
            "reason": "drc_touching_s6a",
            "s6a_board": None,
            "counts": {},
            "copper": {"tracks": 0, "vias": 0},
            "drc_touching_s6a": [{"type": "clearance", "items": []}],
            "pruned_nets": [],
            "report": {},
            "live_taps": [],
        },
    )
    rep2 = pipeline.place_board(src, out_dir=tmp_path / "run2", route=True, s6a=True)
    assert routed_inputs[-1] == Path(rep2["artifacts"]["placed_board"])
    assert rep2["stages"]["s6a"]["applied"] is False
    assert rep2["stages"]["s6a"]["reason"] == "drc_touching_s6a"
    assert "s6a_board" not in rep2["artifacts"]

    # Case 3: s6a=False skips S6a completely
    rep3 = pipeline.place_board(src, out_dir=tmp_path / "run3", route=True, s6a=False)
    assert rep3["stages"]["s6a"] is None
    assert routed_inputs[-1] == Path(rep3["artifacts"]["placed_board"])
