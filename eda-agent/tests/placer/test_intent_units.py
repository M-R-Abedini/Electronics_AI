# SPDX-License-Identifier: MIT
"""F2 design intent: inference on a synthetic board, schema, validation, canonical hash.

Slow (ELECTRONICS_AI_SLOW=1): the dev accuracy gate on the labels, and the
two-client gate: the same intent through two MCP clients gives the same board.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import random
import sys
from pathlib import Path

import jsonschema
import pytest
from eda_agent.placer.eval.metrics import critical_loops_intent
from eda_agent.placer.intent import infer, model, validate
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

SLOW = pytest.mark.skipif(os.environ.get("ELECTRONICS_AI_SLOW") != "1", reason="slow; set ELECTRONICS_AI_SLOW=1")
BOX = [(-1.0, -0.6), (1.0, -0.6), (1.0, 0.6), (-1.0, 0.6)]


def _fp(ref, value, lib, pins, x=0.0, sheet="/"):
    pads = [Pad(str(i + 1), net, (-0.6 + 0.4 * i, 0.0), (0.3, 0.3), layers=("F.Cu",), function=fn, pin_type=pt)
            for i, (net, fn, pt) in enumerate(pins)]
    return Footprint(ref, value, f"lib:{lib}", x, 0.0, 0.0, "F", pads=pads, courtyard=Courtyard("file", [BOX]),
                     uuid="u-" + ref, sheet=sheet)


def test_decap_distance_uses_declared_target_pin():
    chip = _fp("U1", "MCU", "QFN", [("+3V3", "VDD1", "power_in"), ("+3V3", "VDD2", "power_in")])
    cap = _fp("C1", "100nF", "C_0402", [("+3V3", None, "passive")])
    board = Board("pin-target", [chip, cap])
    target = {"ref": "U1", "net": "+3V3", "pins": ["2"]}
    intent = {"parts": {"C1": {"role": {"value": "decap"}, "target": {"value": target}}}}

    assert critical_loops_intent(board, intent)["decap_pin_distance"]["median_mm"] == 0.4


def _board() -> Board:
    p = "passive"
    fps = [
        _fp("U1", "MCU", "QFN-32", [("+3V3", "VDD", "power_in"), ("+3V3", "VDD", "power_in"), ("GND", "GND", "power_in"),
                                    ("XI", "XIN", "input"), ("XO", "XOUT", "output"), ("USB_D+", "D+", "bidirectional"),
                                    ("USB_D-", "D-", "bidirectional"), ("NRST", "NRST", "input")]),
        _fp("C1", "100nF", "C_0402", [("+3V3", None, p), ("GND", None, p)]),
        _fp("C2", "10uF", "C_0805", [("+3V3", None, p), ("GND", None, p)]),
        _fp("Y1", "8MHz", "Crystal_SMD_3225", [("XI", None, p), ("XO", None, p)]),
        _fp("C3", "12pF", "C_0402", [("XI", None, p), ("GND", None, p)]),
        _fp("C4", "12pF", "C_0402", [("XO", None, p), ("GND", None, p)]),
        _fp("U2", "TPS62", "SOT-23-6", [("VIN", "VIN", "power_in"), ("GND", "GND", "power_in"), ("SW", "SW", "output"),
                                         ("FB", "FB", "input"), ("VIN", "EN", "input")]),
        _fp("L1", "2.2uH", "L_0805", [("SW", None, p), ("+3V3", None, p)]),
        _fp("R1", "100k", "R_0402", [("+3V3", None, p), ("FB", None, p)]),
        _fp("R2", "22k", "R_0402", [("FB", None, p), ("GND", None, p)]),
        _fp("C5", "10uF", "C_0805", [("VIN", None, p), ("GND", None, p)]),
        _fp("J1", "USB_C", "USB_C_Receptacle", [("VIN", "VBUS", p), ("USB_D+", "D+", p), ("USB_D-", "D-", p),
                                                 ("GND", "GND", p)]),
        _fp("D1", "TVS", "D_0402", [("GND", "A1", p), ("USB_D+", "A2", p)]),
        _fp("D2", "TVS", "D_0402", [("GND", "A1", p), ("USB_D-", "A2", p)]),
        _fp("R3", "10k", "R_0402", [("+3V3", None, p), ("NRST", None, p)]),
        _fp("H1", "MountingHole", "MountingHole_3.2mm", []),
    ]
    nets = sorted({pad.net for fp in fps for pad in fp.pads if pad.net})
    b = Board(name="synthetic", footprints=fps, outline=[Outline([(0, 0), (30, 0), (30, 30), (0, 30)])],
              board_area_mm2=900.0, copper_layers=[CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("B.Cu", "B.Cu", 1)],
              netclasses=[NetClass("Default", 0.2, 0.2, 0.6, 0.3)], nets=[Net(n, "Default", 2) for n in nets])
    b = derive(b)
    b.source = {"path": "", "sha256": hashlib.sha256(b"synthetic").hexdigest()}
    return b


def _schema():
    return json.loads(model.SCHEMA_FILE.read_text(encoding="utf-8"))


def test_roles_and_targets_on_a_synthetic_board():
    intent = infer.draft(_board())
    role = {r: p["role"]["value"] for r, p in intent["parts"].items()}
    target = {r: (p.get("target") or {}).get("value", {}).get("ref") for r, p in intent["parts"].items()}
    assert role["U1"] == "ic" and role["U2"] == "converter_ic"
    assert (role["C1"], target["C1"]) == ("decap", "U1")
    assert role["Y1"] == "crystal" and target["Y1"] == "U1"
    assert {role["C3"], role["C4"]} == {"crystal_load_cap"} and target["C3"] == "Y1"
    assert (role["L1"], target["L1"]) == ("converter_inductor", "U2")
    assert role["R1"] == role["R2"] == "converter_control"
    assert (role["C5"], target["C5"]) == ("regulator_input_cap", "U2")
    assert (role["D1"], target["D1"]) == ("esd", "J1")
    assert role["R3"] == "pull_up" and role["H1"] == "mechanical" and role["J1"] == "connector"
    pair = intent["diff_pairs"][0]
    assert (pair["value"]["positive"], pair["value"]["negative"], pair["value"]["kind"]) == ("USB_D+", "USB_D-", "usb")
    assert intent["nets"]["SW"]["class"]["value"] == "SW_NODE"
    jsonschema.validate(intent, _schema())


def test_canonical_hash_ignores_order_and_questions():
    intent = infer.draft(_board())
    shuffled = copy.deepcopy(intent)
    items = list(shuffled["parts"].items())
    random.Random(1).shuffle(items)
    shuffled["parts"] = dict(items)
    shuffled["blocks"] = list(reversed(shuffled["blocks"]))
    for block in shuffled["blocks"]:
        block["value"]["members"] = list(reversed(block["value"]["members"]))
    shuffled["questions"] = []
    assert model.intent_hash(shuffled) == model.intent_hash(intent)
    changed = copy.deepcopy(intent)
    changed["parts"]["C1"]["role"]["confidence"] = 0.5
    assert model.intent_hash(changed) != model.intent_hash(intent)


def test_validate_reports_bad_references_and_roles():
    board = _board()
    intent = infer.draft(board)
    assert validate.validate(intent, board)["valid"]
    bad = copy.deepcopy(intent)
    bad["parts"]["C1"]["target"]["value"]["ref"] = "U9"
    bad["parts"]["C3"]["target"]["value"]["ref"] = "U1"  # a load cap serves a crystal, not an IC
    bad["parts"]["C1"]["target"]["value"]["pins"] = ["99"]
    bad["parts"]["ZZ1"] = copy.deepcopy(bad["parts"]["R3"])
    rep = validate.validate(bad, board)
    text = " ".join(rep["errors"])
    assert not rep["valid"]
    assert "U9 is not on the board" in text and "ZZ1" in text and "has role ic" in text
    llm = copy.deepcopy(intent)
    llm["parts"]["C2"]["role"]["source"] = {"kind": "llm"}  # an llm claim needs its evidence
    assert not validate.validate(llm, board)["valid"]


def test_uncertain_target_becomes_a_question():
    board = _board()
    board.footprints.append(_fp("U3", "SENSOR", "QFN-16", [("+3V3", "VDD", "power_in"), ("GND", "GND", "power_in")]))
    intent = infer.draft(board)
    c1 = intent["parts"]["C1"]
    assert c1["role"]["value"] == "decap" and c1["target"]["value"]["ref"] is None
    assert c1["target"]["value"]["candidates"] == ["U1", "U3"] and model.is_uncertain(c1["target"])
    q = next(q for q in intent["questions"] if q.get("ref") == "C1")
    assert q["about"] == "target" and q["critical"]


@SLOW
def test_dev_accuracy_gate():
    from eda_agent.placer import corpus
    from eda_agent.placer.intent import evaluate
    from eda_agent.placer.io.kicad import read_board

    results = {e.name: evaluate.score(infer.draft(read_board(e.path)), evaluate.labels(e.name))
               for e in corpus.entries("dev")}
    total = evaluate.pooled(results)
    assert all(g["passed"] for g in total["gates"]), total["gates"]


async def _place_via_client(client: str, board: str, intent: str, data_dir: str) -> dict:
    from mcp import ClientSession, StdioServerParameters, types
    from mcp.client.stdio import stdio_client

    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    code = ("import sys; from eda_agent.placer import mcp as m; from pathlib import Path; "
            "m.PlacerService.__init__.__defaults__ = (sys.argv[1],); m.serve()")
    params = StdioServerParameters(command=sys.executable, args=["-c", code, data_dir], env=env)
    info = types.Implementation(name=client, version="1")
    async with stdio_client(params) as (read, write), ClientSession(read, write, client_info=info) as s:
        await s.initialize()
        res = await s.call_tool("placer__place_board", {"board_path": board, "intent_path": intent,
                                                         "route": False, "seed": 3})
        return json.loads(res.content[0].text)


@SLOW
def test_same_intent_on_two_clients_gives_the_same_board(tmp_path):
    from eda_agent.placer import corpus
    from eda_agent.placer.io.kicad import read_board
    from eda_agent.placer.toolchain import sha256_file

    entry = corpus.get("pic_programmer")
    intent = infer.draft(read_board(entry.path))
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    model.dump(intent, a)
    reordered = dict(intent, parts=dict(reversed(list(intent["parts"].items()))))
    b.write_text(json.dumps(reordered, indent=3), encoding="utf-8")  # same claims, other layout and order
    runs = [asyncio.run(_place_via_client(c, str(entry.path), str(p), str(tmp_path / c)))
            for c, p in (("claude-code", a), ("codex", b))]
    assert runs[0]["intent"]["sha256"] == runs[1]["intent"]["sha256"]
    assert sha256_file(runs[0]["placed_board"]) == sha256_file(runs[1]["placed_board"])
