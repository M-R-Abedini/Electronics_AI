# SPDX-License-Identifier: MIT
"""design_intent: constants, claims, canonical form and hash (architecture 4.5, P7).

The canonical form is what makes two clients interchangeable: claims are
sorted, lists inside them are sorted, ``questions`` (informational) is left
out, and the JSON is written with sorted keys and no whitespace.  Two intents
that say the same thing in a different order hash the same, so placement
(which reads the intent, never the client) is the same.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

SCHEMA = "electronics-ai/design_intent"
SCHEMA_VERSION = 1
SCHEMA_FILE = Path(__file__).with_name("design_intent.schema.json")

HARD_CONFIDENCE = 0.9  # architecture 3: confirmed or high confidence => hard constraint
UNCERTAIN_CONFIDENCE = 0.6  # below this a claim is reported as uncertain (plan F2)

ROLES = (
    "ic", "converter_ic", "linear_regulator", "power_switch",
    "converter_inductor", "converter_diode", "converter_control",
    "regulator_input_cap", "regulator_output_cap",
    "decap", "bulk_cap", "pin_cap", "coupling_cap",
    "crystal", "oscillator", "crystal_load_cap", "crystal_aux",
    "esd", "connector",
    "series_resistor", "pull_up", "pull_down", "termination", "bias_resistor", "divider_resistor",
    "led", "led_resistor", "rail_filter", "signal_passive",
    "transistor", "diode", "switch", "jumper", "test_point", "mechanical", "unknown",
)

# Roles whose meaning includes what they serve; the value is the role the target must have.
TARGET_ROLES = {
    "decap": None,  # any active part or connector on the rail
    "regulator_input_cap": ("converter_ic", "linear_regulator", "power_switch"),
    "regulator_output_cap": ("converter_ic", "linear_regulator", "power_switch", "ic"),
    "converter_inductor": ("converter_ic",),
    "converter_diode": ("converter_ic",),
    "converter_control": ("converter_ic",),
    "crystal": None,
    "crystal_load_cap": ("crystal",),
    "crystal_aux": ("crystal",),
    "esd": ("connector",),
    "led_resistor": ("led",),
    "pin_cap": None,
    "bias_resistor": None,
}

# Plan F2 gate: decap with its target, crystal, converter parts, ESD (pairs are scored separately).
CRITICAL_ROLES = frozenset({
    "decap", "crystal", "crystal_load_cap", "converter_ic", "converter_inductor", "converter_diode",
    "converter_control", "esd"})
CONVERTER_CAP_ROLES = frozenset({"regulator_input_cap", "regulator_output_cap"})  # critical when the target is a converter

NET_CLASSES = ("POWER_HIGH_CURRENT", "SW_NODE", "DIFF_PAIR", "HIGH_SPEED", "SENSITIVE_ANALOG", "GENERAL")


def claim(value: Any, kind: str, confidence: float, *, ref: str | None = None, detail: str | None = None,
          client: str | None = None, confirmed: bool = False) -> dict:
    source = {"kind": kind}
    if ref:
        source["ref"] = ref
    if client:
        source["client"] = client
    if detail:
        source["detail"] = detail
    return {"value": value, "source": source, "confidence": round(float(confidence), 3),
            "confirmed_by_user": bool(confirmed)}


def is_hard(c: dict | None) -> bool:
    return bool(c) and (c.get("confirmed_by_user") or c.get("confidence", 0.0) >= HARD_CONFIDENCE)


def is_uncertain(c: dict | None) -> bool:
    return not c or (not c.get("confirmed_by_user") and c.get("confidence", 0.0) < UNCERTAIN_CONFIDENCE)


def is_critical(role: str, target_role: str | None = None) -> bool:
    return role in CRITICAL_ROLES or (role in CONVERTER_CAP_ROLES and target_role == "converter_ic")


_NUM = re.compile(r"(\d+)")


def natural_key(s: str) -> list:
    return [int(t) if t.isdigit() else t for t in _NUM.split(str(s))]


def _sorted_unique(items: list) -> list:
    return sorted(set(items), key=natural_key)


def canonical(intent: dict) -> dict:
    """Order-free form of an intent: what the hash and the engine see."""
    doc = copy.deepcopy(intent)
    doc.pop("questions", None)
    for part in doc.get("parts", {}).values():
        target = part.get("target")
        if target and isinstance(target.get("value"), dict):
            for key in ("pins", "candidates"):
                if key in target["value"]:
                    target["value"][key] = _sorted_unique(target["value"][key])
    for block in doc.get("blocks", []):
        block["value"]["members"] = _sorted_unique(block["value"]["members"])
    doc["blocks"] = sorted(doc.get("blocks", []), key=lambda b: natural_key(b["value"]["name"]))
    doc["diff_pairs"] = sorted(doc.get("diff_pairs", []),
                               key=lambda p: (p["value"]["positive"], p["value"]["negative"]))
    for key in ("parts", "nets", "power_w"):
        if key in doc:
            doc[key] = dict(sorted(doc[key].items(), key=lambda kv: natural_key(kv[0])))
    return doc


def dumps_canonical(intent: dict) -> str:
    return json.dumps(canonical(intent), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def intent_hash(intent: dict) -> str:
    return hashlib.sha256(dumps_canonical(intent).encode("utf-8")).hexdigest()


def load(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def dump(intent: dict, path: str | Path) -> Path:
    """Readable file (indented, sorted); the hash never depends on the layout."""
    path = Path(path)
    doc = canonical(intent)
    if intent.get("questions"):
        doc["questions"] = intent["questions"]
    path.write_text(json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def role_of(intent: dict, ref: str) -> str | None:
    part = intent.get("parts", {}).get(ref)
    return part["role"]["value"] if part else None


def target_of(intent: dict, ref: str) -> dict | None:
    part = intent.get("parts", {}).get(ref)
    return part.get("target", {}).get("value") if part and part.get("target") else None
