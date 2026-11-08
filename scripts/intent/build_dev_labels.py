# SPDX-License-Identifier: MIT
"""Build the dev-board role labels (plan F2, task 3: "I label, you review").

Roles are assigned by hand below, part by part, from the schematic facts
(values, pin names, nets) and the role definitions in
``eda-agent/src/eda_agent/placer/intent/LABELS.md``.  Only one thing is
measured instead of typed: the target of a decap (or of an ESD part whose
connector is not unique), which is where the expert actually put it on the
board, the nearest same-rail pin of an IC or connector.  That evidence is
placement, which the inference engine never sees, so the accuracy check is
not circular.

Run: ``pixi run py scripts/intent/build_dev_labels.py`` -> eda-agent/tests/placer/golden/intent/<board>.labels.json
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from eda_agent.placer import corpus
from eda_agent.placer.io.kicad import read_board
from eda_agent.placer.ir import is_ground_net
from eda_agent.placer.toolchain import sha256_file

OUT = Path(__file__).resolve().parents[2] / "eda-agent" / "tests" / "placer" / "golden" / "intent"


def refs(spec: str) -> list[str]:
    """'C1 C5-C10 RR1-RR8' -> refs."""
    out = []
    for tok in spec.split():
        m = re.fullmatch(r"([A-Z]+)(\d+)-[A-Z]*(\d+)", tok)
        if m:
            out += [f"{m.group(1)}{i}" for i in range(int(m.group(2)), int(m.group(3)) + 1)]
        else:
            out.append(tok)
    return out


# role -> parts, optionally "role@TARGET"; decap/esd without a target get the expert-proximity target
LABELS: dict[str, dict[str, str]] = {
    "stickhub": {
        "decap": "C1 C5-C13 C14-C20",
        "regulator_output_cap@U1": "C3 C4",
        "regulator_output_cap@U2": "C22",
        "regulator_input_cap@U2": "C21",
        "pin_cap@U1": "C2",
        "pin_cap@U2": "C28",
        "bulk_cap": "C23-C27 C29-C38",
        "esd": "D1-D14 D22 D23 D24 D25",
        "led": "D15-D21",
        "mechanical": "H1 LOGO1-LOGO4",
        "connector": "J1-J9",
        "jumper": "JP1",
        "divider_resistor": "R1 R2",
        "pull_up": "R3-R5",
        "bias_resistor@U1": "R6",
        "led_resistor": "R7-R13",
        "ic": "U1",
        "power_switch": "U2",
        "crystal@U1": "Y1",
    },
    "pic_programmer": {
        "regulator_output_cap@U3": "C1",
        "regulator_input_cap@U3": "C2",
        "regulator_output_cap@U4": "C3",
        "converter_control@U4": "C4 C5 R10 R15 R16 RV1",
        "decap": "C6 C7",
        "signal_passive": "C9 R17",
        "diode": "D1 D2-D7 D11",
        "led": "D8 D9 D12",
        "converter_diode@U4": "D10",
        "connector": "J1 P1 P2 P3",
        "jumper": "JP1",
        "converter_inductor@U4": "L1",
        "mechanical": "P101-P106",
        "transistor": "Q1-Q3",
        "divider_resistor": "R1-R6",
        "pull_up": "R7 R20",
        "series_resistor": "R8 R11 R12 R13 R18 R19",
        "led_resistor": "R9 R14 R21",
        "ic": "U1 U2 U5 U6",
        "linear_regulator": "U3",
        "converter_ic": "U4",
    },
    "royalblue54L_feather": {
        "decap": "C1 C7 C9 C10 C11 C12 C15 C16 C17 C19 C21 C22 C23 C24 C25",
        "regulator_input_cap@U2": "C2 C3 C5",
        "regulator_output_cap@U2": "C4 C6 C8",
        "signal_passive": "C13 C14 R1",
        "crystal_load_cap@Y2": "C18 C20",
        "led": "D1 D3",
        "mechanical": "H1-H4 LOGO1",
        "connector": "J1-J6 J8",
        "jumper": "JP1",
        "converter_inductor@U2": "L1",
        "pull_up": "R2 R3 R5 R7 R8",
        "converter_control@U2": "R4",
        "led_resistor": "R6",
        "switch": "SW1 SW2",
        "test_point": "TP1-TP12",
        "ic": "U1 U4 U5 U6",
        "converter_ic": "U2",
        "esd@J3": "U3",
        "crystal@U1": "Y1",
        "crystal@U5": "Y2",
    },
    "video": {
        "connector": "BUS1 J4 P1 P2 P3 P5 P8 U12-U19",
        "coupling_cap": "C1 C3 C4 C5 C8 C36 C39 C40 C41 C58 C59 C60 C61",
        "decap": "C2 C9-C15 C17-C31 C33 C70-C73",
        "pin_cap@U10": "C6 C7 C16",
        "pin_cap@U9": "C32 C34 C35 C42",
        "pin_cap@U20": "C43 C44 C45 C48 C64",
        "signal_passive": "C37 C46 C47 C53 L1 L6 POT1 R9 R11 R32 R33 R34",
        "bulk_cap": "C38 C51 C52 C54 C55 C56 C57 C62 C63 C67 C68 C69",
        "crystal_load_cap@X1": "C49 C50",
        "crystal_load_cap@X2": "C65 C66",
        "crystal_load_cap@X3": "CV1",
        "diode": "D1-D4",
        "led": "D6",
        "rail_filter": "L2 L3 L4 L5 R1",
        "jumper": "P4 W1-W5",
        "test_point": "P9-P12",
        "transistor": "Q1-Q3",
        "series_resistor": "R2 R3 R10 R16 R17 R18 R19 R20 R22 R23 R24 R35 R36 R39 R40 R41",
        "pull_up": "R4 R5 R6 R7 R28 R29 R48 RR1-RR8",
        "bias_resistor@U9": "R8",
        "bias_resistor@U10": "R12 R27",
        "bias_resistor@U8": "R37 R38",
        "led_resistor": "R13",
        "pull_down": "R14 R15",
        "crystal_aux@X1": "R21 R26",
        "crystal_aux@X2": "R25",
        "divider_resistor": "R30 R31",
        "termination": "R42-R47",
        "ic": "U1-U11 U20-U24",
        "crystal@U23": "X1",
        "crystal@U10": "X2",
        "crystal@U20": "X3",
    },
    "ecc83": {
        "bulk_cap": "C1",
        "coupling_cap": "C2",
        "connector": "P1-P4",
        "mechanical": "P5-P8",
        "signal_passive": "R1 R2",
        "pull_down": "R3 R4",
        "transistor": "U1",
    },
}

NOTES = {
    ("stickhub", "C14"): "VBUS bypass of the port connector it sits on",
    ("stickhub", "C22"): "load-switch output; the other 22 uF parts are per-port VBUS bulk",
    ("stickhub", "C3"): "XR22417 internal 3.3 V regulator output (pin 3V3_OUT)",
    ("royalblue54L_feather", "C4"): "nPM1300 VSYS output; C2/C3 sit at PVDD (buck input) on the same net",
    ("royalblue54L_feather", "C11"): "nPM1300 VBAT bypass",
    ("video", "C54"): "LC filter output of U20's supply, placed 25 mm from U20: bulk, not a local decap",
    ("pic_programmer", "R1"): "10k/10k RS-232 input dividers with BAT43 clamps",
    ("ecc83", "U1"): "ECC83 valve: a discrete active device",
}

ACTIVE_ROLES = ("ic", "converter_ic", "linear_regulator", "power_switch", "connector")


def nearest_on_rail(board, fp, kinds):
    rail_pad = next((p for p in fp.pads if p.net and not is_ground_net(p.net)), None)
    if rail_pad is None:
        return None, None
    x, y = fp.pad_world(rail_pad)
    best = None
    for other in board.footprints:
        if other.ref == fp.ref or other.ref not in kinds:
            continue
        for pad, (a, b) in other.pads_world():
            if pad.net == rail_pad.net:
                d = math.hypot(a - x, b - y)
                if best is None or d < best[0]:
                    best = (d, other.ref, pad.function or pad.number)
    return best, rail_pad.net


def build(name: str) -> dict:
    entry = corpus.get(name)
    board = read_board(entry.path)
    table: dict[str, tuple[str, str | None]] = {}
    for key, spec in LABELS[name].items():
        role, _, target = key.partition("@")
        for ref in refs(spec):
            if ref in table:
                raise ValueError(f"{name}: {ref} labelled twice")
            table[ref] = (role, target or None)
    present = {fp.ref for fp in board.footprints}
    missing, extra = sorted(present - set(table)), sorted(set(table) - present)
    if missing or extra:
        raise ValueError(f"{name}: unlabelled {missing}, unknown {extra}")
    active = {r for r, (role, _t) in table.items() if role in ACTIVE_ROLES}
    connectors = {r for r, (role, _t) in table.items() if role == "connector"}
    leds = {r for r, (role, _t) in table.items() if role == "led"}
    parts = {}
    for fp in sorted(board.footprints, key=lambda f: (f.prefix, int(re.sub(r"\D", "", f.ref) or 0))):
        role, target = table[fp.ref]
        entry_ = {"role": role}
        evidence = None
        if role == "decap" and not target:
            dec = [p for p in fp.pads if p.net and not is_ground_net(p.net)]
            best, net = nearest_on_rail(board, fp, active)
            if best:
                target, evidence = best[1], f"expert placement: {best[2]} of {best[1]} at {best[0]:.1f} mm on {net}"
            if dec and not best:
                evidence = "no active pin on the rail"
        if role == "esd" and not target:
            nets = {p.net for p in fp.pads if p.net and not is_ground_net(p.net)}
            hit = sorted({o.ref for o in board.footprints if o.ref in connectors for p in o.pads if p.net in nets})
            if len(hit) == 1:
                target, evidence = hit[0], "connector on its net"
            else:
                best, net = nearest_on_rail(board, fp, connectors | {"JP1"} if name == "stickhub" else connectors)
                if best:
                    target, evidence = best[1], f"expert placement: nearest connector pin on {net} at {best[0]:.1f} mm"
        if role == "led_resistor" and not target:
            nets = {p.net for p in fp.pads if p.net}
            target = next(o.ref for o in board.footprints if o.ref in leds for p in o.pads if p.net in nets)
        if target:
            entry_["target"] = target
        if evidence:
            entry_["evidence"] = evidence
        if (name, fp.ref) in NOTES:
            entry_["note"] = NOTES[(name, fp.ref)]
        parts[fp.ref] = entry_
    if name == "stickhub" and parts["D24"].get("target") == "JP1":
        parts["D24"]["target"] = "J1"
        parts["D24"]["evidence"] += " (JP1 bridges VIN to J1's VBUS)"
    pairs = {"stickhub": [["/D+", "/D-"]] + [[f"/U{i}D+", f"/U{i}D-"] for i in range(1, 8)],
             "royalblue54L_feather": [["/Debugger/D+", "/Debugger/D-"]]}.get(name, [])
    return {"board": name, "source_sha256": sha256_file(entry.path), "labelled_by": "Claude (F2), pending user review",
            "definitions": "eda-agent/src/eda_agent/placer/intent/LABELS.md", "parts": parts, "pairs": pairs}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name in LABELS:
        doc = build(name)
        path = OUT / f"{name}.labels.json"
        path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(path, len(doc["parts"]))


if __name__ == "__main__":
    main()
