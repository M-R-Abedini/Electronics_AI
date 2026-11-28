#!/usr/bin/env python3
"""Validate, normalise and scaffold circuit design specs.

The component list is the electrical source of truth.  ``topology`` and
``parameters`` are used by the calculators and by ``scaffold`` to create a
starting component list, but they never override explicit components.

Usage::

    python3 design_spec.py validate specs/rc.json
    python3 design_spec.py explain  specs/rc.json
    python3 design_spec.py scaffold --topology rc_lowpass --param r=1k --param c=100n --out specs/rc.json
    python3 design_spec.py normalize specs/rc.json --out specs/rc.normalized.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from circuit_calc import COMMANDS  # noqa: E402
from circuit_common import (  # noqa: E402
    ValueParseError,
    dumps,
    emit,
    fail,
    parse_value,
    result,
    spice_node_collisions,
    spice_number,
    spice_ref,
)

SCHEMA_VERSION = "1.0"
SUPPORTED_TYPES = {"R", "C", "L", "V", "I", "D", "LED", "ZENER", "NPN", "PNP",
                   "NMOS", "PMOS", "OPAMP", "SW", "E", "G"}
DELIVERABLES = {"spice", "ltspice_asc", "kicad_net", "kicad_sch", "bom", "report"}
# "pcb" is opt-in: it needs board dimensions and is not part of the electrical
# deliverables.  Keep it out of DELIVERABLES so the default set is unchanged.
OPTIONAL_DELIVERABLES = {"pcb"}
ALL_DELIVERABLES = DELIVERABLES | OPTIONAL_DELIVERABLES

# Generic metric extraction.  The audio skill supplies the targets and
# conditions; this table maps each measure to the ngspice analysis that must
# be present in the design spec.
METRIC_MEASURES = {
    "gain_db",
    "bandwidth_hz",
    "bandwidth_low_hz",
    "bandwidth_width_hz",
    "thd_percent",
    "ein_uv",
    "noise_uv",
    "output_vrms",
}
METRIC_ANALYSIS = {
    "gain_db": "ac",
    "bandwidth_hz": "ac",
    "bandwidth_low_hz": "ac",
    "bandwidth_width_hz": "ac",
    "thd_percent": "fourier",
    "ein_uv": "noise",
    "noise_uv": "noise",
    "output_vrms": "tran",
}

BOARDABLE_TYPES = {"R", "C", "L", "D", "LED", "ZENER",
                   "NPN", "PNP", "NMOS", "PMOS", "OPAMP"}
MULTIPIN_TYPES = {"NPN", "PNP", "NMOS", "PMOS", "OPAMP", "SW"}
SPICE_NODE_COUNT = {
    "R": 2, "C": 2, "L": 2, "D": 2, "LED": 2, "ZENER": 2,
    "NPN": 3, "PNP": 3, "NMOS": 4, "PMOS": 4,
    "OPAMP": 5, "SW": 4, "E": 4, "G": 4,
}
# Pin-map defaults are keyed by SPICE element prefix, not component type.
SPICE_PREFIX_NODE_COUNT = {
    "R": 2, "C": 2, "L": 2, "D": 2,
    "Q": 3, "M": 4, "J": 3, "X": 5, "S": 4,
}
SIMULATION_ONLY_TYPES = {"V", "I", "E", "G"}

PIN_ALIASES: dict[str, dict[str, str]] = {
    "R": {"1": "1", "2": "2", "A": "1", "B": "2", "+": "1", "-": "2"},
    "C": {"1": "1", "2": "2", "A": "1", "B": "2", "+": "1", "-": "2"},
    "L": {"1": "1", "2": "2", "A": "1", "B": "2", "+": "1", "-": "2"},
    "V": {"1": "1", "2": "2", "+": "1", "-": "2"},
    "I": {"1": "1", "2": "2", "+": "1", "-": "2"},
    "D": {"1": "1", "2": "2", "A": "1", "K": "2", "ANODE": "1", "CATHODE": "2"},
    "LED": {"1": "1", "2": "2", "A": "1", "K": "2", "ANODE": "1", "CATHODE": "2"},
    "ZENER": {"1": "1", "2": "2", "A": "1", "K": "2", "ANODE": "1", "CATHODE": "2"},
    "NPN": {"1": "1", "2": "2", "3": "3", "C": "1", "B": "2", "E": "3",
            "COLLECTOR": "1", "BASE": "2", "EMITTER": "3"},
    "PNP": {"1": "1", "2": "2", "3": "3", "C": "1", "B": "2", "E": "3",
            "COLLECTOR": "1", "BASE": "2", "EMITTER": "3"},
    "NMOS": {"1": "1", "2": "2", "3": "3", "D": "1", "G": "2", "S": "3",
             "DRAIN": "1", "GATE": "2", "SOURCE": "3"},
    "PMOS": {"1": "1", "2": "2", "3": "3", "D": "1", "G": "2", "S": "3",
             "DRAIN": "1", "GATE": "2", "SOURCE": "3"},
    "OPAMP": {"1": "1", "2": "2", "3": "3", "4": "4", "5": "5",
              "IN+": "1", "+": "1", "IN-": "2", "-": "2",
              "V+": "3", "V-": "4", "OUT": "5"},
    "SW": {"1": "1", "2": "2", "3": "3", "4": "4", "A": "1", "B": "2",
           "NC+": "3", "NC-": "4"},
    "E": {"1": "1", "2": "2", "3": "3", "4": "4", "+": "1", "-": "2",
          "CP": "3", "CN": "4"},
    "G": {"1": "1", "2": "2", "3": "3", "4": "4", "+": "1", "-": "2",
          "CP": "3", "CN": "4"},
}

REQUIRED_PINS: dict[str, set[str]] = {
    "R": {"1", "2"}, "C": {"1", "2"}, "L": {"1", "2"},
    "V": {"1", "2"}, "I": {"1", "2"},
    "D": {"1", "2"}, "LED": {"1", "2"}, "ZENER": {"1", "2"},
    "NPN": {"1", "2", "3"}, "PNP": {"1", "2", "3"},
    "NMOS": {"1", "2", "3"}, "PMOS": {"1", "2", "3"},
    "OPAMP": {"1", "2", "5"}, "SW": {"1", "2", "3", "4"},
    "E": {"1", "2", "3", "4"}, "G": {"1", "2", "3", "4"},
}


def canonical_pin(ctype: str, pin: Any) -> str:
    """Map a pin name or alias to the numeric key used in the spec."""
    mapping = PIN_ALIASES.get(ctype.upper(), {})
    key = str(pin).upper()
    return mapping.get(key, mapping.get(str(pin), str(pin)))

# Typical generic models.  These are deliberately conservative and are always
# reported as generic/unverified.
GENERIC_MODELS: dict[str, dict[str, Any]] = {
    "D": {"is": 1e-14, "n": 1.0, "rs": 0.1, "cjo": 2e-12},
    "LED": {"is": 1e-20, "n": 1.8, "rs": 5.0, "cjo": 1e-11},
    "ZENER": {"is": 1e-14, "n": 1.0, "bv": 5.1, "ibv": 0.05, "rs": 5.0},
    "NPN": {"is": 1e-14, "bf": 150, "vaf": 100, "cjc": 4e-12, "cje": 8e-12, "tf": 0.4e-9},
    "PNP": {"is": 1e-14, "bf": 100, "vaf": 80, "cjc": 6e-12, "cje": 10e-12, "tf": 0.5e-9},
    "NMOS": {"vto": 2.0, "kp": 0.02, "lambda": 0.01, "cgs": 5e-12, "cgd": 1e-12},
    "PMOS": {"vto": -2.0, "kp": 0.01, "lambda": 0.01, "cgs": 8e-12, "cgd": 2e-12},
}


def requires_model(ctype: str) -> bool:
    """True when a device needs a SPICE model card to simulate."""
    return ctype in GENERIC_MODELS or ctype in ("SW", "OPAMP")


def uses_generic_model(spec: dict[str, Any], comp: dict[str, Any]) -> bool:
    """True when a device falls back to a generated placeholder model.

    A component is only backed by a user model when it names one *and* that
    model is declared in ``spec["models"]``.  Naming an undeclared model still
    produces a generic card, so it must count as unverified.
    """
    if not requires_model(comp["type"]):
        return False
    declared = spec.get("models") or {}
    requested = comp.get("model")
    return not (requested and requested in declared)


def is_structured_model(raw: Any) -> bool:
    """A model entry that carries a card or a file, rather than raw SPICE params."""
    return isinstance(raw, dict) and ("file" in raw or "card" in raw)


def _extract_model_block(text: str, name: str) -> tuple[str | None, bool]:
    """Pull one ``.model`` or ``.subckt`` block out of a vendor library."""
    subckt = re.compile(rf"^\s*\.subckt\s+{re.escape(name)}\b", re.IGNORECASE | re.MULTILINE)
    match = subckt.search(text)
    if match:
        start = text.rfind("\n", 0, match.start()) + 1
        depth = 1
        end = len(text)
        pos = match.end()
        while pos < len(text):
            newline = text.find("\n", pos)
            line_end = len(text) if newline == -1 else newline + 1
            line = text[pos:line_end]
            if re.match(r"^\s*\.subckt\b", line, re.IGNORECASE):
                depth += 1
            elif re.match(r"^\s*\.ends\b", line, re.IGNORECASE):
                depth -= 1
                if depth == 0:
                    end = line_end
                    break
            pos = line_end
        return text[start:end].strip(), True

    model = re.compile(rf"^\s*\.model\s+{re.escape(name)}\b", re.IGNORECASE | re.MULTILINE)
    match = model.search(text)
    if not match:
        return None, False
    start = text.rfind("\n", 0, match.start()) + 1
    depth = 0
    opened = False
    i = match.end()
    while i < len(text):
        char = text[i]
        if char == "(":
            depth += 1
            opened = True
        elif char == ")":
            depth -= 1
            if opened and depth == 0:
                end = text.find("\n", i)
                return text[start:end if end != -1 else len(text)].strip(), False
        elif char == "\n" and not opened:
            # A following line beginning with '+' continues the card.
            rest = text[i + 1:].lstrip("\r\n \t")
            if rest.startswith("+"):
                i += 1
                continue
            return text[start:i].strip(), False
        i += 1
    return text[start:].strip(), False


def resolve_model(spec: dict[str, Any], name: str | None,
                  base_dir: str | Path | None = None) -> dict[str, Any] | None:
    """Resolve a declared model to card text plus provenance.

    Returns ``None`` when the model is not declared, or when the declaration
    uses the legacy parameter-object form (which ``generate.py`` expands).
    """
    if not name:
        return None
    declared = spec.get("models") or {}
    raw = declared.get(name)
    if raw is None:
        return None

    info: dict[str, Any] = {
        "name": name, "card": None, "kind": "declared", "subckt": False,
        "vendor": None, "part": None, "source": None, "sha256": None,
        "file": None, "subckt_pins": None, "error": None,
    }

    if isinstance(raw, str):
        info["card"] = raw.strip()
        info["subckt"] = bool(re.search(r"^\s*\.subckt\b", info["card"], re.IGNORECASE | re.MULTILINE))
        return info

    if not is_structured_model(raw):
        return None  # legacy parameter object; generate.py builds the card

    info["vendor"] = raw.get("vendor")
    info["part"] = raw.get("part")
    info["source"] = raw.get("source")
    info["subckt_pins"] = raw.get("subckt_pins")
    lookup = str(raw.get("name") or name)
    info["name"] = lookup

    if raw.get("file"):
        info["file"] = str(raw["file"])
        path = Path(str(raw["file"]))
        if not path.is_absolute() and base_dir is None:
            # Caller cannot resolve the spec directory; classify from metadata
            # only. Validation and generation always pass base_dir.
            if info["vendor"] and (info["part"] or info["source"]):
                info["kind"] = "vendor"
            return info
        if not path.is_absolute() and base_dir is not None:
            path = Path(base_dir) / path
        try:
            data = path.read_bytes()
        except OSError as exc:
            info["error"] = f"cannot read model file {path}: {exc}"
            return info
        actual = hashlib.sha256(data).hexdigest()
        declared_hash = raw.get("sha256")
        if declared_hash and str(declared_hash).lower() != actual:
            info["error"] = (f"sha256 mismatch for {path}: declared {declared_hash}, "
                             f"actual {actual}")
            return info
        info["sha256"] = actual
        card, is_subckt = _extract_model_block(data.decode("utf-8", errors="replace"), lookup)
        if card is None:
            info["error"] = (f"{path} does not define .model or .subckt {lookup!r}")
            return info
        info["card"] = card
        info["subckt"] = is_subckt
    else:
        info["card"] = str(raw["card"]).strip()
        info["subckt"] = bool(re.search(r"^\s*\.subckt\b", info["card"],
                                        re.IGNORECASE | re.MULTILINE))
        if raw.get("sha256"):
            info["sha256"] = str(raw["sha256"]).lower()

    if info["vendor"] and (info["part"] or info["source"]):
        info["kind"] = "vendor"
    return info


def model_records(spec: dict[str, Any],
                  base_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """One record per semiconductor, including unresolved placeholders."""
    records: list[dict[str, Any]] = []
    for comp in spec["components"]:
        if not requires_model(comp["type"]):
            continue
        requested = comp.get("model")
        resolved = resolve_model(spec, requested, base_dir)
        records.append({
            "ref": comp["ref"],
            "type": comp["type"],
            "model": requested,
            "generic": uses_generic_model(spec, comp),
            "resolved": resolved,
        })
    return records


def model_status(spec: dict[str, Any], base_dir: str | Path | None = None) -> str:
    """Human-readable simulation provenance for the report and verifier."""
    modelled = [c for c in spec["components"] if requires_model(c["type"])]
    if not modelled:
        return "not applicable (no semiconductor models)"
    if any(uses_generic_model(spec, c) for c in modelled):
        return "unverified (generic models)"
    resolved = [resolve_model(spec, c.get("model"), base_dir) or {} for c in modelled]
    if any(info.get("error") for info in resolved):
        return "model errors (see validation)"
    if any(info.get("kind") != "vendor" for info in resolved):
        return "declared models (no vendor provenance)"
    return "vendor models supplied"


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def normalize_pins(ctype: str, pins: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    ctype = ctype.upper()
    aliases = PIN_ALIASES.get(ctype, {})
    out: dict[str, str] = {}
    warnings: list[str] = []
    for key, net in pins.items():
        key_norm = aliases.get(str(key).upper(), aliases.get(str(key), None))
        if key_norm is None:
            warnings.append(f"unknown pin {key!r} for {ctype}; kept as-is")
            key_norm = str(key)
        if not isinstance(net, str) or not net.strip():
            raise ValueError(f"pin {key!r} has an empty net name")
        if key_norm in out and out[key_norm] != net:
            raise ValueError(f"pin {key_norm} is assigned to both {out[key_norm]!r} and {net!r}")
        out[key_norm] = net.strip()
    return out, warnings


def normalize_component(comp: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    if not isinstance(comp, dict):
        raise ValueError("component must be an object")
    for field in ("ref", "type", "pins"):
        if field not in comp:
            raise ValueError(f"component is missing {field!r}")
    ctype = str(comp["type"]).upper()
    if ctype not in SUPPORTED_TYPES:
        raise ValueError(f"unsupported component type {comp['type']!r}")
    ref = str(comp["ref"])
    pins, pin_warnings = normalize_pins(ctype, comp["pins"])
    warnings.extend(pin_warnings)

    value = comp.get("value", 0.0)
    if isinstance(value, str):
        try:
            value, value_warnings = parse_value(value, name=f"{ref}.value")
            warnings.extend(value_warnings)
        except ValueParseError as exc:
            raise ValueError(str(exc)) from exc
    value = float(value)
    if ctype not in ("V", "I") and value < 0:
        raise ValueError(f"{ref}: value must be non-negative")
    if ctype in ("R", "C", "L") and value <= 0:
        raise ValueError(f"{ref}: value must be positive")

    out: dict[str, Any] = {
        "ref": ref,
        "type": ctype,
        "value": value,
        "pins": pins,
    }
    for key in ("footprint", "model", "notes", "params", "waveform", "manufacturer", "mpn", "package", "datasheet_url", "datasheet_revision", "sourcing_url", "footprint_status", "footprint_evidence", "pad_map_verified"):
        if key in comp:
            out[key] = comp[key]
    if "placement" in comp:
        placement = comp["placement"]
        if not isinstance(placement, dict) or set(placement) != {"x_mm", "y_mm"}:
            raise ValueError(f"{ref}: placement must contain only x_mm and y_mm")
        x, y = placement["x_mm"], placement["y_mm"]
        if any(not isinstance(v, (int, float)) or isinstance(v, bool)
               or not math.isfinite(float(v)) or float(v) <= 0 for v in (x, y)):
            raise ValueError(f"{ref}: placement coordinates must be finite and positive")
        grid = 1.27
        if (not math.isclose(float(x) / grid, round(float(x) / grid), abs_tol=1e-6)
                or not math.isclose(float(y) / grid, round(float(y) / grid), abs_tol=1e-6)):
            raise ValueError(f"{ref}: placement must align to the KiCad 1.27 mm grid")
        out["placement"] = {"x_mm": float(x), "y_mm": float(y)}
    if "tolerance_percent" in comp:
        tolerance = comp["tolerance_percent"]
        if (not isinstance(tolerance, (int, float)) or isinstance(tolerance, bool)
                or not math.isfinite(float(tolerance))
                or float(tolerance) < 0 or float(tolerance) >= 100):
            raise ValueError(f"{ref}: tolerance_percent must be finite and in [0, 100)")
        out["tolerance_percent"] = float(tolerance)
    if out.get("footprint_status") not in (None, "verified", "provisional", "unassigned"):
        raise ValueError(f"{ref}: footprint_status must be verified, provisional, or unassigned")
    if "pad_map_verified" in out and not isinstance(out["pad_map_verified"], bool):
        raise ValueError(f"{ref}: pad_map_verified must be boolean")
    if out.get("pad_map_verified") and not out.get("footprint_evidence"):
        raise ValueError(f"{ref}: verified pad map requires footprint_evidence")
    for key in ("dc", "ac", "gain", "rout"):
        if key in comp:
            out[key] = float(comp[key])
    if ctype == "V" and "dc" not in out and value != 0:
        out["dc"] = value
    if ctype == "I" and "dc" not in out and value != 0:
        out["dc"] = value
    return out, warnings


def normalize_spec(spec: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(spec, dict):
        raise ValueError("design spec must be a JSON object")
    warnings: list[str] = []
    out: dict[str, Any] = dict(spec)
    out["schema_version"] = str(spec.get("schema_version", SCHEMA_VERSION))
    out["ground"] = str(spec.get("ground", "GND"))
    out.setdefault("description", "")
    out.setdefault("topology", "custom")
    out.setdefault("parameters", {})
    out.setdefault("supplies", [])
    out.setdefault("models", {})
    out.setdefault("assumptions", [])
    out.setdefault("notes", [])
    out.setdefault("metrics", {})

    comps = []
    for comp in spec.get("components", []):
        normalized, warns = normalize_component(comp)
        comps.append(normalized)
        warnings.extend(warns)
    out["components"] = comps

    deliverables = spec.get("deliverables") or sorted(DELIVERABLES)
    if not isinstance(deliverables, list):
        raise ValueError("deliverables must be a list")
    unknown = [d for d in deliverables if d not in ALL_DELIVERABLES]
    if unknown:
        raise ValueError(f"unknown deliverable(s): {', '.join(map(str, unknown))}")
    out["deliverables"] = deliverables

    analyses = spec.get("analyses")
    if not analyses:
        analyses = [{"type": "op"}]
        if any(c["type"] in ("V", "I") and (c.get("ac") or spec.get("input", {}).get("type") == "ac")
               for c in comps):
            analyses.append({"type": "ac", "start": 10.0, "stop": 1e6, "points": 100})
        if any(c["type"] in ("SW",) for c in comps):
            analyses.append({"type": "tran", "step": 1e-6, "stop": 1e-3})
    out["analyses"] = analyses

    env = dict(spec.get("environment") or {})
    env.setdefault("ta_c", 25.0)
    env.setdefault("tj_max_c", 125.0)
    out["environment"] = env

    verification = dict(spec.get("verification") or {})
    verification.setdefault("tolerance_percent", 2.0)
    out["verification"] = verification
    return out, warnings


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_PCB_KEYS = {
    "enabled", "profile", "title", "revision", "board", "rules", "netclasses",
    "nets", "footprints", "pin_map", "node_aliases", "placement", "routing",
    "zones", "fabrication", "signoff", "extra_components", "assumptions",
    "notes",
}
_PCB_BOARD_KEYS = {
    "width_mm", "height_mm", "layers", "thickness_mm", "copper_oz",
    "material", "surface_finish", "mask_color", "silkscreen_color",
    "edge_radius_mm", "stackup",
}
_PCB_PINMAP_KEYS = {"refs", "defaults", "include", "exclude"}


def _pcb_pad_list(value: Any) -> list[str] | None:
    """Expand a pin-map value into pad strings, or None if malformed."""
    if isinstance(value, (list, tuple)):
        pads = [str(pad).strip() for pad in value]
    else:
        pads = [str(value).strip()]
    if not pads or any(not pad for pad in pads):
        return None
    return pads


def _pcb_mapping_pad_count(mapping: Any) -> int | None:
    """Count the pads in a pin-map entry, or None if it cannot be counted."""
    if isinstance(mapping, (list, tuple)):
        return len(mapping)
    if not isinstance(mapping, dict):
        return None
    if mapping and all(str(key).isdigit() for key in mapping):
        # Ordered numeric map: key is the SPICE node index, value is the pad.
        return len(mapping)
    total = 0
    for value in mapping.values():
        pads = _pcb_pad_list(value)
        if pads is None:
            return None
        total += len(pads)
    return total


def validate_pcb(spec: dict[str, Any], comps: list[dict[str, Any]],
                 nets: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    """Validate the optional pcb-design-skill handoff block.

    This checks the parts that would otherwise fail late in the PCB pipeline:
    board dimensions, footprint syntax, pin-map size and net aliases.  It does
    not replace the PCB skill's own DRC/DFM gates.
    """
    errors: list[str] = []
    warnings: list[str] = []
    raw = spec.get("pcb")
    if raw is None:
        return errors, warnings
    if not isinstance(raw, dict):
        return ["pcb must be an object"], warnings

    unknown = sorted(set(raw) - _PCB_KEYS)
    if unknown:
        errors.append(f"pcb has unknown key(s): {', '.join(unknown)}")
    if "enabled" in raw and not isinstance(raw["enabled"], bool):
        errors.append("pcb.enabled must be a boolean")
    for key in ("title", "revision", "profile"):
        if key in raw and raw[key] is not None and not isinstance(raw[key], str):
            errors.append(f"pcb.{key} must be a string")
    for key in ("rules", "netclasses", "placement", "routing", "fabrication", "signoff"):
        if key in raw and raw[key] is not None and not isinstance(raw[key], dict):
            errors.append(f"pcb.{key} must be an object")
    for key in ("zones", "assumptions", "notes", "extra_components"):
        if key in raw and raw[key] is not None and not isinstance(raw[key], list):
            errors.append(f"pcb.{key} must be a list")
    if "nets" in raw and raw["nets"] is not None:
        if not isinstance(raw["nets"], list):
            errors.append("pcb.nets must be a list")
        else:
            for item in raw["nets"]:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str) \
                        or not item["name"]:
                    errors.append("each pcb.nets entry needs a non-empty name")
                elif any(ch.isspace() for ch in item["name"]):
                    errors.append(f"pcb.nets name {item['name']!r} has whitespace")
    for key in ("assumptions", "notes"):
        if isinstance(raw.get(key), list):
            for item in raw[key]:
                if not isinstance(item, str):
                    errors.append(f"pcb.{key} entries must be strings")

    by_ref = {c["ref"]: c for c in comps}
    by_spice = {spice_ref(c["ref"], c["type"]): c for c in comps}

    def component_for(ref: str) -> dict[str, Any] | None:
        return by_ref.get(ref) or by_spice.get(ref)

    placement = raw.get("placement")
    if isinstance(placement, dict):
        mode = placement.get("mode")
        if mode is not None and mode not in ("auto", "fixed"):
            errors.append("pcb.placement.mode must be 'auto' or 'fixed'")
        at_map = placement.get("components")
        if at_map is not None:
            if not isinstance(at_map, dict):
                errors.append("pcb.placement.components must be an object")
            else:
                for ref, at in at_map.items():
                    if component_for(str(ref)) is None:
                        warnings.append(
                            f"pcb.placement.components references unknown "
                            f"component {ref!r}"
                        )
                    if (not isinstance(at, dict)
                            or "x_mm" not in at or "y_mm" not in at):
                        errors.append(
                            f"pcb.placement.components.{ref} needs x_mm and y_mm"
                        )
    board = raw.get("board")
    if board is not None:
        if not isinstance(board, dict):
            errors.append("pcb.board must be an object")
        else:
            bad = sorted(set(board) - _PCB_BOARD_KEYS)
            if bad:
                errors.append(f"pcb.board has unknown key(s): {', '.join(bad)}")
            for key in ("width_mm", "height_mm"):
                value = board.get(key)
                if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                    errors.append(f"pcb.board.{key} must be > 0")
            layers = board.get("layers")
            if layers is not None:
                if (not isinstance(layers, int) or isinstance(layers, bool)
                        or layers < 1 or layers > 32):
                    errors.append("pcb.board.layers must be an integer in 1..32")
            for key in ("thickness_mm", "copper_oz"):
                value = board.get(key)
                if value is not None and (
                        not isinstance(value, (int, float)) or isinstance(value, bool)
                        or value <= 0):
                    errors.append(f"pcb.board.{key} must be > 0")
            radius = board.get("edge_radius_mm")
            if radius is not None and (
                    not isinstance(radius, (int, float)) or isinstance(radius, bool)
                    or radius < 0):
                errors.append("pcb.board.edge_radius_mm must be >= 0")
    else:
        warnings.append(
            "pcb.board is not set; the handoff needs a board size "
            "(--board / --pcb-board) or a pcb.board block"
        )

    profile = raw.get("profile")
    if profile is not None and (not isinstance(profile, str) or not profile.strip()):
        errors.append("pcb.profile must be a non-empty string")

    footprints = raw.get("footprints")
    if footprints is not None:
        if not isinstance(footprints, dict):
            errors.append("pcb.footprints must be an object mapping ref to footprint")
        else:
            for ref, footprint in footprints.items():
                if component_for(str(ref)) is None:
                    warnings.append(f"pcb.footprints references unknown component {ref!r}")
                if not isinstance(footprint, str) or not footprint.strip():
                    errors.append(f"pcb.footprints.{ref} must be a non-empty string")
                elif ":" not in footprint and not footprint.endswith(".kicad_mod"):
                    errors.append(
                        f"pcb.footprints.{ref} must be 'Library:Footprint' "
                        "or a .kicad_mod path"
                    )

    raw_pin_map = raw.get("pin_map")
    raw_exclude: list[str] = []
    if isinstance(raw_pin_map, dict) and set(raw_pin_map) & _PCB_PINMAP_KEYS:
        candidate = raw_pin_map.get("exclude") or []
        if isinstance(candidate, list):
            raw_exclude = [str(item) for item in candidate]
    excluded_elements: set[str] = set()
    for item in raw_exclude:
        comp = component_for(item)
        if comp is not None:
            excluded_elements.add(spice_ref(comp["ref"], comp["type"]))

    for comp in comps:
        ref, ctype = comp["ref"], comp["type"]
        element = spice_ref(ref, ctype)
        footprint = comp.get("footprint") or (
            (footprints.get(ref) or footprints.get(element))
            if isinstance(footprints, dict) else ""
        )
        if (ctype in BOARDABLE_TYPES and element not in excluded_elements
                and not footprint):
            warnings.append(
                f"{ref} ({ctype}) has no footprint; the PCB pipeline will ask "
                "for one or fail with --no-ask"
            )
        elif isinstance(footprint, str) and footprint and ":" not in footprint \
                and not footprint.endswith(".kicad_mod"):
            errors.append(
                f"{ref} footprint must be 'Library:Footprint' or a .kicad_mod path"
            )

    pin_map = raw.get("pin_map")
    refs: dict[str, Any] = {}
    defaults: dict[str, Any] = {}
    include: list[str] = []
    exclude: list[str] = []
    if pin_map is not None:
        if not isinstance(pin_map, dict):
            errors.append("pcb.pin_map must be an object")
        else:
            if set(pin_map) & _PCB_PINMAP_KEYS:
                bad = sorted(set(pin_map) - _PCB_PINMAP_KEYS)
                if bad:
                    errors.append(f"pcb.pin_map has unknown key(s): {', '.join(bad)}")
                refs = pin_map.get("refs") or {}
                defaults = pin_map.get("defaults") or {}
                include = list(pin_map.get("include") or [])
                exclude = list(pin_map.get("exclude") or [])
                if not isinstance(refs, dict):
                    errors.append("pcb.pin_map.refs must be an object")
                    refs = {}
                if not isinstance(defaults, dict):
                    errors.append("pcb.pin_map.defaults must be an object")
                    defaults = {}
                for key, value in (("include", include), ("exclude", exclude)):
                    if not isinstance(value, list):
                        errors.append(f"pcb.pin_map.{key} must be a list")
                for item in exclude:
                    comp = component_for(str(item))
                    if (comp is not None
                            and comp["type"] not in SIMULATION_ONLY_TYPES
                            and comp["type"] != "SW"):
                        warnings.append(
                            f"{comp['ref']} is a boardable part but was "
                            "explicitly excluded; the PCB will not contain it"
                        )
            else:
                # Bare {ref: mapping} is accepted for convenience.
                refs = pin_map

            for ref, mapping in refs.items():
                comp = component_for(str(ref))
                if comp is None:
                    warnings.append(f"pcb.pin_map.refs references unknown component {ref!r}")
                    continue
                count = _pcb_mapping_pad_count(mapping)
                expected = SPICE_NODE_COUNT.get(comp["type"])
                if count is None:
                    errors.append(f"pcb.pin_map.refs.{ref} must be a list or object of pads")
                elif expected is not None and count != expected:
                    errors.append(
                        f"pcb.pin_map.refs.{ref} has {count} pad(s); the SPICE "
                        f"netlist has {expected} node(s) for a {comp['type']}"
                    )
            for prefix, mapping in defaults.items():
                key = str(prefix).upper()
                expected = SPICE_PREFIX_NODE_COUNT.get(key)
                if expected is None:
                    warnings.append(f"pcb.pin_map.defaults has unknown prefix {prefix!r}")
                count = _pcb_mapping_pad_count(mapping)
                if count is None:
                    errors.append(f"pcb.pin_map.defaults.{prefix} must be a list or object of pads")
                elif expected is not None and count != expected:
                    errors.append(
                        f"pcb.pin_map.defaults.{prefix} has {count} pad(s); "
                        f"a {prefix} element has {expected} node(s)"
                    )
            # A pad-count check cannot see which net each pad carries.
            # Reuse the handoff normaliser to catch a pad shorted to two
            # different nets before a handoff is written.
            try:
                from pcb_handoff import (  # noqa: PLC0415
                    check_mapping_pads,
                    normalise_mapping,
                )
            except ImportError:
                check_mapping_pads = None
            if check_mapping_pads is not None:
                for ref, mapping in refs.items():
                    comp = component_for(str(ref))
                    if comp is None or comp["type"] in SIMULATION_ONLY_TYPES:
                        continue
                    element = spice_ref(comp["ref"], comp["type"])
                    if (comp["type"] == "SW" and comp["ref"] not in include
                            and element not in include):
                        continue
                    try:
                        normalised = normalise_mapping(
                            comp["type"], mapping, str(ref)
                        )
                        check_mapping_pads(comp, normalised, str(ref))
                    except ValueError as exc:
                        message = str(exc)
                        prefix = f"{ref}: "
                        if message.startswith(prefix):
                            message = message[len(prefix):]
                        errors.append(f"pcb.pin_map.refs.{ref}: {message}")

            known_refs = {c["ref"] for c in comps} | set(by_spice)
            for ref in include + exclude:
                if ref not in known_refs:
                    warnings.append(f"pcb.pin_map list references unknown component {ref!r}")

    included = {str(ref) for ref in include}
    for comp in comps:
        ref, ctype = comp["ref"], comp["type"]
        element = spice_ref(ref, ctype)
        if ctype in SIMULATION_ONLY_TYPES:
            continue
        if ctype == "SW" and ref not in included and element not in included:
            continue
        if (ctype in MULTIPIN_TYPES and ref not in refs and element not in refs
                and element[:1].upper() not in defaults):
            # The PCB skill can ask, so this is a warning, not an error.
            warnings.append(
                f"{ref} ({ctype}) has no pcb.pin_map entry; the PCB pipeline "
                "will ask for a package pinout"
            )

    aliases = raw.get("node_aliases")
    if aliases is not None:
        if not isinstance(aliases, dict):
            errors.append("pcb.node_aliases must be an object")
        else:
            for node, net_name in aliases.items():
                if not isinstance(node, str) or not node:
                    errors.append("pcb.node_aliases keys must be non-empty strings")
                if not isinstance(net_name, str) or not net_name:
                    errors.append(f"pcb.node_aliases[{node!r}] must be a non-empty string")
                elif any(ch.isspace() for ch in net_name):
                    errors.append(f"pcb.node_aliases[{node!r}] must not contain whitespace")
            if "0" in aliases and aliases["0"] != spec["ground"]:
                errors.append(
                    f'pcb.node_aliases["0"] must be the ground net '
                    f'{spec["ground"]!r}, not {aliases["0"]!r}'
                )

    placement = raw.get("placement")
    if placement is not None and not isinstance(placement, dict):
        errors.append("pcb.placement must be an object")
    routing = raw.get("routing")
    if routing is not None and not isinstance(routing, dict):
        errors.append("pcb.routing must be an object")

    extra = raw.get("extra_components")
    if extra is not None:
        if not isinstance(extra, list):
            errors.append("pcb.extra_components must be a list")
        else:
            seen: set[str] = set()
            for item in extra:
                if not isinstance(item, dict):
                    errors.append("each pcb.extra_components entry must be an object")
                    continue
                ref = item.get("ref")
                if not isinstance(ref, str) or not ref:
                    errors.append("pcb.extra_components entries need a non-empty ref")
                    continue
                if ref in by_ref or ref in by_spice or ref in seen:
                    errors.append(f"pcb.extra_components ref {ref!r} is duplicated")
                seen.add(ref)
                footprint = item.get("footprint")
                if not isinstance(footprint, str) or not footprint:
                    errors.append(f"pcb.extra_components {ref} needs a footprint")
                pads = item.get("pads")
                if not isinstance(pads, dict):
                    errors.append(f"pcb.extra_components {ref} needs a pads object")
                else:
                    for pad, net_name in pads.items():
                        if not isinstance(net_name, str) or not net_name:
                            errors.append(f"pcb.extra_components {ref} pad {pad} has no net")
                        elif any(ch.isspace() for ch in net_name):
                            errors.append(
                                f"pcb.extra_components {ref} pad {pad} net has whitespace"
                            )
                        elif net_name not in nets and net_name != spec["ground"]:
                            warnings.append(
                                f"pcb.extra_components {ref} pad {pad} uses net "
                                f"{net_name!r}, which is not in the circuit netlist"
                            )

    return errors, warnings


def validate_design_record(design: Any) -> tuple[list[str], list[str]]:
    """Shape-check the planning record and flag unsourced claims.

    The record is optional.  When present, malformed shapes are errors and
    missing evidence is a warning; ``verify.py --require design`` turns the
    missing-evidence cases into a failure.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if design is None:
        return errors, warnings
    if not isinstance(design, dict):
        return ["'design' must be an object"], warnings

    sections: dict[str, list[Any]] = {}
    for section in ("requirements", "decisions", "risks", "sources"):
        value = design.get(section, [])
        if not isinstance(value, list):
            errors.append(f"design.{section} must be a list")
            value = []
        sections[section] = value
    architecture = design.get("architecture")
    if architecture is not None and not isinstance(architecture, dict):
        errors.append("design.architecture must be an object")
    elif isinstance(architecture, dict):
        for key in ("blocks", "interfaces", "test_points"):
            if key in architecture and not isinstance(architecture[key], list):
                errors.append(f"design.architecture.{key} must be a list")
        if "budgets" in architecture and not isinstance(architecture["budgets"], dict):
            errors.append("design.architecture.budgets must be an object")

    for index, item in enumerate(sections["requirements"]):
        if not isinstance(item, dict):
            errors.append(f"design.requirements[{index}] must be an object")
            continue
        if not item.get("id") or not item.get("text"):
            errors.append(f"design.requirements[{index}] needs id and text")
        if not item.get("source"):
            warnings.append(
                f"requirement {item.get('id', index)!r} has no source; "
                "cite the brief, standard, or measurement"
            )

    for index, item in enumerate(sections["decisions"]):
        if not isinstance(item, dict):
            errors.append(f"design.decisions[{index}] must be an object")
            continue
        if not item.get("id") or not item.get("decision"):
            errors.append(f"design.decisions[{index}] needs id and decision")
        if not item.get("rationale"):
            warnings.append(
                f"decision {item.get('id', index)!r} has no rationale"
            )
        if not item.get("sources"):
            warnings.append(
                f"decision {item.get('id', index)!r} cites no source; "
                "a datasheet, app note, or measurement is required for a "
                "part or architecture choice"
            )

    for index, item in enumerate(sections["risks"]):
        if not isinstance(item, dict):
            errors.append(f"design.risks[{index}] must be an object")
            continue
        if not item.get("id") or not item.get("risk"):
            errors.append(f"design.risks[{index}] needs id and risk")
        severity = item.get("severity")
        if severity is not None and severity not in ("low", "medium", "high"):
            errors.append(
                f"design.risks[{index}].severity must be low, medium or high"
            )
        if severity == "high" and not (item.get("mitigation") and item.get("verification")):
            warnings.append(
                f"high risk {item.get('id', index)!r} needs a mitigation and a verification"
            )

    for index, item in enumerate(sections["sources"]):
        if not isinstance(item, dict):
            errors.append(f"design.sources[{index}] must be an object")
            continue
        if not item.get("title"):
            errors.append(f"design.sources[{index}] needs a title")
        if not item.get("url") and not item.get("document"):
            warnings.append(
                f"source {item.get('title', index)!r} has neither url nor document"
            )
        if item.get("url") and not item.get("accessed"):
            warnings.append(
                f"source {item.get('title', index)!r} has a url but no accessed date"
            )
    return errors, warnings


def _metric_variable(value: Any) -> str:
    text = str(value).strip().lower()
    if "(" not in text:
        return f"v({text})"
    return text


def validate_metrics(metrics: Any, analyses: list[dict[str, Any]],
                     design: Any) -> tuple[list[str], list[str]]:
    """Validate the generic metric block and its analysis/requirement links."""
    errors: list[str] = []
    warnings: list[str] = []
    if metrics in (None, {}):
        return errors, warnings
    if not isinstance(metrics, dict):
        return ["metrics must be an object"], warnings
    extra = set(metrics) - {"conditions", "metrics"}
    if extra:
        warnings.append(f"metrics has unknown key(s): {', '.join(sorted(extra))}")
    items = metrics.get("metrics")
    if not isinstance(items, list) or not items:
        return ["metrics.metrics must be a non-empty list"], warnings

    analysis_by_type = {
        item.get("type"): item for item in analyses
        if isinstance(item, dict) and item.get("type")
    }
    requirements = design.get("requirements") if isinstance(design, dict) else None
    requirement_ids = {
        item.get("id") for item in requirements
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    } if isinstance(requirements, list) else set()
    seen_ids: set[str] = set()

    for index, metric in enumerate(items):
        where = f"metrics.metrics[{index}]"
        if not isinstance(metric, dict):
            errors.append(f"{where} must be an object")
            continue
        measure = metric.get("measure")
        if measure not in METRIC_MEASURES:
            errors.append(
                f"{where}: unknown measure {measure!r}; supported: "
                + ", ".join(sorted(METRIC_MEASURES)))
            continue
        expected = METRIC_ANALYSIS[measure]
        analysis = metric.get("analysis", expected)
        if analysis != expected:
            errors.append(
                f"{where}: {measure} uses the {expected!r} analysis, not {analysis!r}")
        if expected not in analysis_by_type:
            errors.append(f"{where}: add a {expected!r} entry to 'analyses'")

        output = metric.get("output")
        if not isinstance(output, str) or not output.strip():
            errors.append(f"{where}: 'output' is required")

        if measure in ("gain_db", "bandwidth_hz", "bandwidth_low_hz",
                       "bandwidth_width_hz"):
            source = metric.get("input")
            if not isinstance(source, str) or not source.strip():
                errors.append(f"{where}: 'input' is required for {measure}")
            if "frequency" in metric:
                frequency = metric["frequency"]
                if (not isinstance(frequency, (int, float))
                        or isinstance(frequency, bool) or frequency <= 0):
                    errors.append(f"{where}: frequency must be a positive number")
            elif measure == "gain_db":
                warnings.append(f"{where}: no frequency; gain defaults to 1 kHz")

        if measure == "thd_percent":
            fundamental = metric.get("fundamental")
            if (not isinstance(fundamental, (int, float))
                    or isinstance(fundamental, bool) or fundamental <= 0):
                errors.append(
                    f"{where}: 'fundamental' must be a positive number for THD")
            if "tran" not in analysis_by_type:
                errors.append(f"{where}: THD also needs a 'tran' entry in 'analyses'")
            fourier = analysis_by_type.get("fourier")
            if fourier is not None:
                if (fourier.get("output") and output
                        and _metric_variable(fourier["output"])
                        != _metric_variable(output)):
                    warnings.append(
                        f"{where}: fourier output {fourier['output']!r} differs "
                        f"from metric output {output!r}")
                if (fundamental and fourier.get("fundamental")
                        and abs(float(fourier["fundamental"])
                                - float(fundamental)) > 1e-9):
                    warnings.append(
                        f"{where}: fourier fundamental {fourier['fundamental']} "
                        f"differs from metric fundamental {fundamental}")

        if measure == "output_vrms":
            if "tran" not in analysis_by_type:
                errors.append(
                    f"{where}: output_vrms needs a 'tran' entry in 'analyses'")
            start, stop = metric.get("start"), metric.get("stop")
            if (start is None) != (stop is None):
                warnings.append(f"{where}: specify both start and stop, or neither")
            if start is not None and stop is not None:
                if not all(
                        isinstance(value, (int, float))
                        and not isinstance(value, bool)
                        for value in (start, stop)):
                    errors.append(f"{where}: start and stop must be numbers")
                elif not (0 <= start < stop):
                    errors.append(f"{where}: require 0 <= start < stop")

        if measure in ("ein_uv", "noise_uv"):
            noise = analysis_by_type.get("noise")
            if noise is None:
                errors.append(f"{where}: add a 'noise' entry to 'analyses'")
            elif (output and noise.get("output")
                  and _metric_variable(noise["output"])
                  != _metric_variable(output)):
                warnings.append(
                    f"{where}: noise output {noise['output']!r} differs from "
                    f"metric output {output!r}")

        if "harmonics" in metric:
            harmonics = metric["harmonics"]
            if (not isinstance(harmonics, int) or isinstance(harmonics, bool)
                    or harmonics < 2):
                errors.append(f"{where}: harmonics must be an integer >= 2")

        has_target = "target" in metric
        has_limits = "max" in metric or "min" in metric
        exploratory = metric.get("exploratory") is True
        if not has_target and not has_limits and not exploratory:
            errors.append(
                f"{where}: needs target+tolerance, min, max, or exploratory=true")
        if has_target:
            target = metric["target"]
            if not isinstance(target, (int, float)) or isinstance(target, bool):
                errors.append(f"{where}: target must be a number")
            if "tolerance" not in metric:
                errors.append(f"{where}: target needs a tolerance")
            else:
                tolerance = metric["tolerance"]
                if (not isinstance(tolerance, (int, float))
                        or isinstance(tolerance, bool) or tolerance < 0):
                    errors.append(
                        f"{where}: tolerance must be a non-negative number")
        if has_target and has_limits:
            warnings.append(
                f"{where}: both target and a limit are present; target wins")
        if exploratory and (has_target or has_limits):
            warnings.append(
                f"{where}: exploratory=true ignores the target or limit")
        for key in ("min", "max"):
            if key in metric:
                value = metric[key]
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    errors.append(f"{where}: {key} must be a number")

        requirement = metric.get("requirement")
        if requirement is not None:
            if not isinstance(requirement, str) or not requirement.strip():
                errors.append(
                    f"{where}: requirement must be a non-empty string")
            elif requirement not in requirement_ids:
                errors.append(
                    f"{where}: requirement {requirement!r} is absent from "
                    f"design.requirements")

        identifier = metric.get("id")
        if identifier is not None:
            if not isinstance(identifier, str) or not identifier.strip():
                errors.append(f"{where}: id must be a non-empty string")
            elif identifier in seen_ids:
                errors.append(f"{where}: duplicate metric id {identifier!r}")
            else:
                seen_ids.add(identifier)
    return errors, warnings


def validate_spec(spec: dict[str, Any], base_dir: str | Path | None = None) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        norm, norm_warnings = normalize_spec(spec)
        warnings.extend(norm_warnings)
    except (ValueError, TypeError) as exc:
        return {"ok": False, "errors": [str(exc)], "warnings": warnings,
                "stats": {}}

    name = spec.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("name is required and must be a non-empty string")
    elif not all(ch.isalnum() or ch in "._-" for ch in name):
        errors.append("name may contain only letters, digits, dot, underscore and hyphen")

    comps = norm["components"]
    refs: set[str] = set()
    nets: dict[str, list[str]] = {}
    for comp in comps:
        ref, ctype = comp["ref"], comp["type"]
        if ref in refs:
            errors.append(f"duplicate reference {ref!r}")
        refs.add(ref)
        missing = REQUIRED_PINS[ctype] - set(comp["pins"])
        if missing:
            errors.append(f"{ref} ({ctype}) is missing pin(s): {', '.join(sorted(missing))}")
        for net in comp["pins"].values():
            nets.setdefault(net, []).append(ref)
        if ctype in ("NPN", "PNP", "NMOS", "PMOS", "D", "LED", "ZENER") and not comp.get("model"):
            warnings.append(
                f"{ref} ({ctype}) has no model; a generic model will be used and "
                f"simulation results are unverified"
            )
        if ctype == "OPAMP" and not {"3", "4"} <= set(comp["pins"]):
            warnings.append(f"{ref}: supply pins 3/4 are not connected; MNA/SPICE will not model supplies")

    for net in nets:
        if not net:
            errors.append("net names must not be empty")
        elif any(ch.isspace() for ch in net):
            errors.append(
                f"net name {net!r} contains whitespace; use an underscore. "
                "Whitespace is not portable to SPICE node names or PCB net names"
            )
    collisions = spice_node_collisions(nets, norm["ground"])
    for node, names in collisions.items():
        errors.append(
            f"nets {', '.join(repr(n) for n in names)} all map to SPICE node "
            f"{node!r}; rename them so every net has a unique node"
        )
    element_names: dict[str, str] = {}
    for comp in comps:
        element = spice_ref(comp["ref"], comp["type"])
        key = element.lower()
        if key in element_names:
            errors.append(
                f"components {element_names[key]!r} and {comp['ref']!r} both "
                f"map to SPICE element {element!r}; rename one of them"
            )
        element_names[key] = comp["ref"]

    ground = norm["ground"]
    if ground not in nets:
        errors.append(f"ground net {ground!r} is not connected to any component")
    if not any(c["type"] in ("V", "I") for c in comps):
        errors.append("at least one independent source (V or I) is required for simulation")

    # Nets with only one pin are almost always a wiring mistake, except for
    # explicit test points / hierarchical labels.
    for net, connected in nets.items():
        if net == ground:
            continue
        if len(connected) < 2:
            warnings.append(
                f"net {net!r} connects only {connected[0]}; it is electrically floating"
            )

    # Supply declarations should agree with the actual source values when both exist.
    supply_names = {s.get("name") for s in norm["supplies"] if isinstance(s, dict)}
    for supply in norm["supplies"]:
        if not isinstance(supply, dict) or "name" not in supply or "voltage" not in supply:
            errors.append("each supply needs name and voltage")
    for comp in comps:
        if comp["type"] == "V" and comp.get("dc") is not None:
            for net, refs_for_net in nets.items():
                if comp["ref"] in refs_for_net and net in supply_names:
                    declared = next(
                        (s["voltage"] for s in norm["supplies"] if s.get("name") == net),
                        None,
                    )
                    if declared is not None and abs(declared - comp["dc"]) > 1e-9:
                        warnings.append(
                            f"supply {net!r} declares {declared} V but {comp['ref']} "
                            f"sets {comp['dc']} V"
                        )

    for analysis in norm["analyses"]:
        atype = analysis.get("type")
        if atype not in ("op", "ac", "tran", "dc", "noise", "tf", "fourier"):
            errors.append(f"unknown analysis type {atype!r}")
        if atype == "ac":
            if not analysis.get("start") or not analysis.get("stop"):
                errors.append("ac analysis needs start and stop")
            elif analysis["start"] <= 0 or analysis["stop"] <= analysis["start"]:
                errors.append("ac analysis requires 0 < start < stop")
        if atype == "tran":
            if not analysis.get("step") or not analysis.get("stop"):
                errors.append("tran analysis needs step and stop")
            elif analysis["step"] <= 0 or analysis["stop"] <= 0:
                errors.append("tran step and stop must be positive")
        if atype == "fourier":
            fundamental = analysis.get("fundamental")
            if not isinstance(fundamental, (int, float)) or fundamental <= 0:
                errors.append("fourier analysis needs a positive fundamental")

    if not any(c["type"] in ("V", "I") and (c.get("ac") or c.get("waveform"))
               for c in comps):
        warnings.append(
            "no AC or transient excitation is defined; only the DC operating point can be simulated"
        )

    # Model references must resolve.
    for comp in comps:
        model = comp.get("model")
        if model and model not in norm["models"] and comp["type"] in GENERIC_MODELS:
            warnings.append(
                f"{comp['ref']} references model {model!r}, which is not defined in "
                f"'models'; a generic model card will be emitted instead"
            )

    # Structured model entries: file provenance, hash, and subcircuit pin order.
    for model_name, raw in (norm["models"] or {}).items():
        if not is_structured_model(raw):
            continue
        info = resolve_model(norm, model_name, base_dir)
        assert info is not None
        if info.get("error"):
            errors.append(f"model {model_name!r}: {info['error']}")
            continue
        card_text = str(info.get("card") or "")
        if not re.search(r"^\s*\.(model|subckt)\b", card_text, re.IGNORECASE | re.MULTILINE):
            errors.append(
                f"model {model_name!r}: card does not contain a .model or .subckt definition"
            )
            continue
        if not info.get("vendor") or not (info.get("part") or info.get("source")):
            warnings.append(
                f"model {model_name!r} has no vendor/part/source provenance; "
                f"the report will call it declared, not vendor"
            )
        if not info.get("subckt"):
            continue
        users = [c for c in comps if c.get("model") == model_name]
        for comp in users:
            order = comp.get("subckt_pins") or raw.get("subckt_pins")
            if not isinstance(order, list) or not order:
                errors.append(
                    f"{comp['ref']} uses subcircuit model {model_name!r}; "
                    f"add 'subckt_pins' listing its node order (e.g. [\"1\", \"2\", \"3\"])"
                )
                continue
            mapped = [canonical_pin(comp["type"], pin) for pin in order]
            missing = [pin for pin in mapped if pin not in comp["pins"]]
            if missing:
                errors.append(
                    f"{comp['ref']}: subckt_pins refers to missing pin(s) "
                    f"{', '.join(map(str, missing))}"
                )

    design_errors, design_warnings = validate_design_record(norm.get("design"))
    errors.extend(design_errors)
    warnings.extend(design_warnings)

    metric_errors, metric_warnings = validate_metrics(
        norm.get("metrics"), norm["analyses"], norm.get("design"))
    errors.extend(metric_errors)
    warnings.extend(metric_warnings)

    pcb_errors, pcb_warnings = validate_pcb(norm, comps, nets)
    errors.extend(pcb_errors)
    warnings.extend(pcb_warnings)

    stats = {
        "components": len(comps),
        "nets": len(nets),
        "sources": sum(1 for c in comps if c["type"] in ("V", "I")),
        "semiconductors": sum(1 for c in comps if c["type"] in GENERIC_MODELS),
        "pcb": bool(norm.get("pcb")),
    }
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "stats": stats,
        "normalized": norm,
    }


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------

def _param(params: dict[str, Any], key: str, default: Any = None) -> Any:
    value = params.get(key, default)
    if isinstance(value, str):
        return parse_value(value, name=key)[0]
    return value


def _require(params: dict[str, Any], key: str) -> float:
    value = _param(params, key)
    if value is None:
        raise ValueError(f"topology requires parameter {key!r}")
    return float(value)


def _base(name: str, topology: str, description: str, params: dict[str, Any],
          components: list[dict[str, Any]], analyses: list[dict[str, Any]],
          ground: str = "GND", supplies: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "description": description,
        "topology": topology,
        "ground": ground,
        "supplies": supplies or [],
        "parameters": {k: _param(params, k) for k in params},
        "components": components,
        "analyses": analyses,
        "deliverables": sorted(DELIVERABLES),
        "assumptions": [],
    }


def scaffold(topology: str, params: dict[str, Any], name: str | None = None) -> dict[str, Any]:
    """Build a starting spec for a known topology."""
    topo = topology.lower().replace("-", "_")
    name = name or topo

    if topo in ("rc_lowpass", "rc_highpass"):
        r = _require(params, "r")
        c = _require(params, "c")
        vin = float(_param(params, "vin", 1.0) or 1.0)
        high = topo.endswith("highpass")
        comps = [
            {"ref": "V1", "type": "V", "value": 0.0, "dc": vin, "ac": 1.0,
             "pins": {"1": "IN", "2": "GND"}},
        ]
        if high:
            comps += [
                {"ref": "C1", "type": "C", "value": c, "pins": {"1": "IN", "2": "OUT"}},
                {"ref": "R1", "type": "R", "value": r, "pins": {"1": "OUT", "2": "GND"}},
            ]
            if params.get("rload") is not None:
                comps.append({"ref": "RL1", "type": "R", "value": _param(params, "rload"),
                              "pins": {"1": "OUT", "2": "GND"}})
        else:
            comps += [
                {"ref": "R1", "type": "R", "value": r, "pins": {"1": "IN", "2": "OUT"}},
                {"ref": "C1", "type": "C", "value": c, "pins": {"1": "OUT", "2": "GND"}},
            ]
        return _base(name, topo, f"RC {'high' if high else 'low'}-pass filter", params, comps,
                     [{"type": "op"}, {"type": "ac", "start": 10.0, "stop": 1e6, "points": 200}])

    if topo == "divider":
        vin = _require(params, "vin")
        r1 = _require(params, "r1")
        r2 = _require(params, "r2")
        comps = [
            {"ref": "V1", "type": "V", "value": 0.0, "dc": vin, "pins": {"1": "IN", "2": "GND"}},
            {"ref": "R1", "type": "R", "value": r1, "pins": {"1": "IN", "2": "OUT"}},
            {"ref": "R2", "type": "R", "value": r2, "pins": {"1": "OUT", "2": "GND"}},
        ]
        return _base(name, topo, "Resistive voltage divider", params, comps,
                     [{"type": "op"}, {"type": "ac", "start": 10.0, "stop": 1e6, "points": 50}])

    if topo in ("sallen_key_lp", "sallen_key_hp", "sallen_key_lowpass", "sallen_key_highpass"):
        high = topo.endswith(("_hp", "_highpass")) or "high" in topo
        fc = _require(params, "fc")
        q = float(_param(params, "q", 0.7071067811865476) or 0.7071067811865476)
        gain = float(_param(params, "gain", 1.0) or 1.0)
        c2 = _param(params, "c2")
        c = _param(params, "c")
        calc_params = {"topology": "highpass" if high else "lowpass", "fc": fc, "q": q, "gain": gain}
        if c2 is not None:
            calc_params["c2"] = c2
        if c is not None:
            calc_params["c"] = c
        design = COMMANDS["sallen-key"]["fn"](calc_params)
        if not design.get("ok"):
            raise ValueError(design.get("error", "Sallen-Key design failed"))
        r = design["results"]
        vcc = float(_param(params, "vcc", 5.0) or 5.0)
        vee = float(_param(params, "vee", -5.0) if params.get("vee") is not None else -5.0)
        comps = [
            {"ref": "V1", "type": "V", "value": 0.0, "dc": 0.0, "ac": 1.0,
             "pins": {"1": "IN", "2": "GND"}},
        ]
        if high:
            comps += [
                {"ref": "C1", "type": "C", "value": r["c1_F"], "pins": {"1": "IN", "2": "A"}},
                {"ref": "C2", "type": "C", "value": r["c2_F"], "pins": {"1": "A", "2": "P"}},
                {"ref": "R1", "type": "R", "value": r["r1_ohm"], "pins": {"1": "A", "2": "OUT"}},
                {"ref": "R2", "type": "R", "value": r["r2_ohm"], "pins": {"1": "P", "2": "GND"}},
            ]
        else:
            comps += [
                {"ref": "R1", "type": "R", "value": r["r1_ohm"], "pins": {"1": "IN", "2": "A"}},
                {"ref": "R2", "type": "R", "value": r["r2_ohm"], "pins": {"1": "A", "2": "P"}},
                {"ref": "C1", "type": "C", "value": r["c1_F"], "pins": {"1": "A", "2": "OUT"}},
                {"ref": "C2", "type": "C", "value": r["c2_F"], "pins": {"1": "P", "2": "GND"}},
            ]
        if abs(gain - 1.0) < 1e-9:
            comps.append({"ref": "U1", "type": "OPAMP", "value": 1e6,
                          "pins": {"1": "P", "2": "OUT", "3": "VCC", "4": "VEE", "5": "OUT"}})
        else:
            rb = float(_param(params, "rb", 10e3) or 10e3)
            ra = (gain - 1.0) * rb
            comps += [
                {"ref": "U1", "type": "OPAMP", "value": 1e6,
                 "pins": {"1": "P", "2": "N", "3": "VCC", "4": "VEE", "5": "OUT"}},
                {"ref": "Ra", "type": "R", "value": ra, "pins": {"1": "OUT", "2": "N"}},
                {"ref": "Rb", "type": "R", "value": rb, "pins": {"1": "N", "2": "GND"}},
            ]
        comps += [
            {"ref": "V2", "type": "V", "value": 0.0, "dc": vcc, "pins": {"1": "VCC", "2": "GND"}},
            {"ref": "V3", "type": "V", "value": 0.0, "dc": vee, "pins": {"1": "VEE", "2": "GND"}},
        ]
        return _base(name, topo, f"Sallen-Key {'high' if high else 'low'}-pass filter",
                     params, comps,
                     [{"type": "op"}, {"type": "ac", "start": 10.0, "stop": 1e6, "points": 300}],
                     supplies=[{"name": "VCC", "voltage": vcc}, {"name": "VEE", "voltage": vee}])

    if topo in ("bjt_ce", "bjt_common_emitter"):
        vcc = _require(params, "vcc")
        ic = _require(params, "ic")
        beta = float(_param(params, "beta", 100) or 100)
        re = _require(params, "re")
        rc = _param(params, "rc")
        vce = _param(params, "vce")
        calc_params = {"vcc": vcc, "ic": ic, "beta": beta, "re": re,
                       "stiffness": float(_param(params, "stiffness", 10) or 10)}
        if rc is not None:
            calc_params["rc"] = rc
        if vce is not None:
            calc_params["vce"] = vce
        design = COMMANDS["bjt-bias"]["fn"](calc_params)
        if not design.get("ok"):
            raise ValueError(design.get("error", "BJT bias design failed"))
        r = design["results"]
        rload = float(_param(params, "rload", 10e3) or 10e3)
        cin = float(_param(params, "cin", 10e-6) or 10e-6)
        cout = float(_param(params, "cout", 10e-6) or 10e-6)
        comps = [
            {"ref": "V1", "type": "V", "value": 0.0, "dc": vcc, "pins": {"1": "VCC", "2": "GND"}},
            {"ref": "V2", "type": "V", "value": 0.0, "dc": 0.0, "ac": 1.0, "pins": {"1": "IN", "2": "GND"}},
            {"ref": "R1", "type": "R", "value": r["r1_ohm"], "pins": {"1": "VCC", "2": "B"}},
            {"ref": "R2", "type": "R", "value": r["r2_ohm"], "pins": {"1": "B", "2": "GND"}},
            {"ref": "RC1", "type": "R", "value": (vcc - r["vc_V"]) / r["ic_A"],
             "pins": {"1": "VCC", "2": "C"}},
            {"ref": "RE1", "type": "R", "value": re, "pins": {"1": "E", "2": "GND"}},
            {"ref": "Q1", "type": "NPN", "value": 0.0, "model": "QGEN",
             "pins": {"1": "C", "2": "B", "3": "E"}},
            {"ref": "CIN1", "type": "C", "value": cin, "pins": {"1": "IN", "2": "B"}},
            {"ref": "COUT1", "type": "C", "value": cout, "pins": {"1": "C", "2": "OUT"}},
            {"ref": "RL1", "type": "R", "value": rload, "pins": {"1": "OUT", "2": "GND"}},
        ]
        return _base(name, topo, "BJT common-emitter amplifier", params, comps,
                     [{"type": "op"}, {"type": "ac", "start": 10.0, "stop": 1e6, "points": 200}],
                     supplies=[{"name": "VCC", "voltage": vcc}],
                     )

    if topo == "led":
        vs = _require(params, "vs")
        vf = _require(params, "vf")
        if_ = _require(params, "if")
        n = int(_param(params, "n", 1) or 1)
        design = COMMANDS["led"]["fn"]({"vs": vs, "vf": vf, "if": if_, "n": n})
        if not design.get("ok"):
            raise ValueError(design.get("error", "LED design failed"))
        r = design["results"]["series_resistance_ohm"]
        comps = [
            {"ref": "V1", "type": "V", "value": 0.0, "dc": vs, "pins": {"1": "VCC", "2": "GND"}},
            {"ref": "R1", "type": "R", "value": r, "pins": {"1": "VCC", "2": "A"}},
        ]
        for i in range(n):
            a = "A" if i == 0 else f"A{i}"
            k = "GND" if i == n - 1 else f"A{i+1}"
            comps.append({"ref": f"D{i+1}", "type": "LED", "value": vf,
                          "model": "LEDGEN", "pins": {"1": a, "2": k}})
        return _base(name, topo, "LED with series resistor", params, comps,
                     [{"type": "op"}], supplies=[{"name": "VCC", "voltage": vs}])

    if topo == "i2c":
        vdd = _require(params, "vdd")
        rp = _require(params, "rp")
        cb = float(_param(params, "cb", 200e-12) or 200e-12)
        comps = [
            {"ref": "V1", "type": "V", "value": 0.0, "dc": vdd, "pins": {"1": "VDD", "2": "GND"}},
            {"ref": "RP1", "type": "R", "value": rp, "pins": {"1": "VDD", "2": "SDA"}},
            {"ref": "RP2", "type": "R", "value": rp, "pins": {"1": "VDD", "2": "SCL"}},
            {"ref": "CB1", "type": "C", "value": cb, "pins": {"1": "SDA", "2": "GND"}},
            {"ref": "CB2", "type": "C", "value": cb, "pins": {"1": "SCL", "2": "GND"}},
        ]
        return _base(name, topo, "I2C bus pull-ups and capacitance", params, comps,
                     [{"type": "op"}, {"type": "ac", "start": 1e3, "stop": 1e8, "points": 100}],
                     supplies=[{"name": "VDD", "voltage": vdd}])

    if topo in ("opamp_inverting", "inverting"):
        gain = abs(_require(params, "gain"))
        rin = _require(params, "rin")
        rf = gain * rin
        vcc = float(_param(params, "vcc", 5.0) or 5.0)
        vee = float(_param(params, "vee", -5.0) if params.get("vee") is not None else -5.0)
        rload = _param(params, "rload")
        comps = [
            {"ref": "V1", "type": "V", "value": 0.0, "dc": 0.0, "ac": 1.0,
             "pins": {"1": "IN", "2": "GND"}},
            {"ref": "RIN1", "type": "R", "value": rin, "pins": {"1": "IN", "2": "N"}},
            {"ref": "RF1", "type": "R", "value": rf, "pins": {"1": "OUT", "2": "N"}},
            {"ref": "U1", "type": "OPAMP", "value": 1e6,
             "pins": {"1": "GND", "2": "N", "3": "VCC", "4": "VEE", "5": "OUT"}},
            {"ref": "V2", "type": "V", "value": 0.0, "dc": vcc,
             "pins": {"1": "VCC", "2": "GND"}},
            {"ref": "V3", "type": "V", "value": 0.0, "dc": vee,
             "pins": {"1": "VEE", "2": "GND"}},
        ]
        if rload is not None:
            comps.append({"ref": "RL1", "type": "R", "value": rload,
                          "pins": {"1": "OUT", "2": "GND"}})
        return _base(name, topo, "Inverting op-amp amplifier", params, comps,
                     [{"type": "op"}, {"type": "ac", "start": 10.0, "stop": 1e6, "points": 200}],
                     supplies=[{"name": "VCC", "voltage": vcc}, {"name": "VEE", "voltage": vee}])

    if topo in ("opamp_noninverting", "noninverting"):
        gain = _require(params, "gain")
        rg = float(_param(params, "rg", 10e3) or 10e3)
        rf = (gain - 1.0) * rg
        vcc = float(_param(params, "vcc", 5.0) or 5.0)
        vee = float(_param(params, "vee", -5.0) if params.get("vee") is not None else -5.0)
        comps = [
            {"ref": "V1", "type": "V", "value": 0.0, "dc": 0.0, "ac": 1.0, "pins": {"1": "IN", "2": "GND"}},
            {"ref": "RF1", "type": "R", "value": rf, "pins": {"1": "OUT", "2": "N"}},
            {"ref": "RG1", "type": "R", "value": rg, "pins": {"1": "N", "2": "GND"}},
            {"ref": "U1", "type": "OPAMP", "value": 1e6,
             "pins": {"1": "IN", "2": "N", "3": "VCC", "4": "VEE", "5": "OUT"}},
            {"ref": "V2", "type": "V", "value": 0.0, "dc": vcc, "pins": {"1": "VCC", "2": "GND"}},
            {"ref": "V3", "type": "V", "value": 0.0, "dc": vee, "pins": {"1": "VEE", "2": "GND"}},
        ]
        return _base(name, topo, "Non-inverting op-amp amplifier", params, comps,
                     [{"type": "op"}, {"type": "ac", "start": 10.0, "stop": 1e6, "points": 200}],
                     supplies=[{"name": "VCC", "voltage": vcc}, {"name": "VEE", "voltage": vee}])

    if topo in ("zener_regulator", "zener"):
        vin = _require(params, "vin")
        vz = _require(params, "vz")
        rs = _require(params, "rs")
        rload = float(_param(params, "rload", 1e3) or 1e3)
        comps = [
            {"ref": "V1", "type": "V", "value": 0.0, "dc": vin, "pins": {"1": "VIN", "2": "GND"}},
            {"ref": "RS1", "type": "R", "value": rs, "pins": {"1": "VIN", "2": "OUT"}},
            {"ref": "D1", "type": "ZENER", "value": vz, "model": "ZENERGEN",
             "pins": {"1": "GND", "2": "OUT"}},
            {"ref": "RL1", "type": "R", "value": rload, "pins": {"1": "OUT", "2": "GND"}},
        ]
        return _base(name, topo, "Zener shunt regulator", params, comps,
                     [{"type": "op"}], supplies=[{"name": "VIN", "voltage": vin}])

    if topo in ("buck", "buck_converter"):
        vin = _require(params, "vin")
        vout = _require(params, "vout")
        iout = _require(params, "iout")
        fsw = _require(params, "fsw")
        design = COMMANDS["buck"]["fn"]({"vin": vin, "vout": vout, "iout": iout,
                                         "fsw": fsw, "ripple": float(_param(params, "ripple", 0.3) or 0.3)})
        if not design.get("ok"):
            raise ValueError(design.get("error", "buck design failed"))
        r = design["results"]
        l = float(_param(params, "l", r["inductor_required_H"]) or r["inductor_required_H"])
        cout = float(_param(params, "cout", r["output_cap_required_F"]) or r["output_cap_required_F"])
        duty = r["duty_cycle"]
        rload = vout / iout
        comps = [
            {"ref": "V1", "type": "V", "value": 0.0, "dc": vin, "pins": {"1": "VIN", "2": "GND"}},
            {"ref": "S1", "type": "SW", "value": 0.0, "model": "SWMOD",
             "pins": {"1": "VIN", "2": "SW", "3": "PWM", "4": "GND"}},
            {"ref": "D1", "type": "D", "value": 0.0, "model": "DGEN",
             "pins": {"1": "GND", "2": "SW"}},
            {"ref": "L1", "type": "L", "value": l, "pins": {"1": "SW", "2": "OUT"}},
            {"ref": "C1", "type": "C", "value": cout, "pins": {"1": "OUT", "2": "GND"}},
            {"ref": "RL1", "type": "R", "value": rload, "pins": {"1": "OUT", "2": "GND"}},
            {"ref": "V2", "type": "V", "value": 0.0,
             "waveform": {"type": "pulse", "params": [0.0, 5.0, 0.0, 1e-9, 1e-9, duty / fsw, 1.0 / fsw]},
             "pins": {"1": "PWM", "2": "GND"}},
        ]
        return _base(name, topo, "Buck converter power stage", params, comps,
                     [{"type": "tran", "step": 1.0 / (fsw * 1000), "stop": 20.0 / fsw}],
                     supplies=[{"name": "VIN", "voltage": vin}])

    raise ValueError(
        f"no scaffold for topology {topology!r}; supply an explicit component list. "
        f"Scaffolds: rc_lowpass, rc_highpass, divider, sallen_key_lp, sallen_key_hp, opamp_inverting, "
        f"bjt_ce, led, i2c, opamp_noninverting, zener_regulator, buck"
    )


# ---------------------------------------------------------------------------
# Summary / CLI
# ---------------------------------------------------------------------------

def summary(spec: dict[str, Any]) -> str:
    norm, _ = normalize_spec(spec)
    lines = [f"Design: {norm.get('name')}  ({norm.get('topology')})"]
    if norm.get("description"):
        lines.append(f"  {norm['description']}")
    lines.append(f"  ground: {norm['ground']}")
    if norm.get("supplies"):
        sup = ", ".join(f"{s['name']}={s['voltage']} V" for s in norm["supplies"])
        lines.append(f"  supplies: {sup}")
    lines.append("  components:")
    for c in norm["components"]:
        value = c["value"]
        if c["type"] in ("R", "C", "L"):
            value_text = spice_number(value) + {"R": "Ω", "C": "F", "L": "H"}[c["type"]]
        elif c["type"] in ("V", "I"):
            value_text = f"dc={c.get('dc', 0)} ac={c.get('ac', 0)}"
        elif c["type"] in ("D", "LED", "ZENER"):
            value_text = f"Vf/Vz={value} V"
        elif c["type"] in ("NPN", "PNP", "NMOS", "PMOS"):
            value_text = c.get("model", "generic")
        elif c["type"] == "OPAMP":
            value_text = f"Aol={value}"
        else:
            value_text = str(value)
        pins = " ".join(f"{k}={v}" for k, v in sorted(c["pins"].items()))
        lines.append(f"    {c['ref']:<6} {c['type']:<6} {value_text:<24} {pins}")
    lines.append(f"  analyses: {', '.join(a.get('type','?') for a in norm['analyses'])}")
    lines.append(f"  deliverables: {', '.join(norm['deliverables'])}")
    return "\n".join(lines)


def load_spec(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("design spec must be a JSON object")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="design_spec.py",
                                     description="Validate and scaffold circuit design specs.")
    sub = parser.add_subparsers(dest="command")

    p_val = sub.add_parser("validate", help="validate a design spec")
    p_val.add_argument("spec")
    p_val.add_argument("--json", action="store_true")

    p_exp = sub.add_parser("explain", help="print a human-readable summary")
    p_exp.add_argument("spec")

    p_norm = sub.add_parser("normalize", help="write a normalized spec")
    p_norm.add_argument("spec")
    p_norm.add_argument("--out", required=True)

    p_scaf = sub.add_parser("scaffold", help="create a spec for a known topology")
    p_scaf.add_argument("--topology", required=True)
    p_scaf.add_argument("--name")
    p_scaf.add_argument("--param", action="append", default=[],
                        metavar="KEY=VALUE", help="topology parameter (repeatable)")
    p_scaf.add_argument("--out", required=True)

    ns = parser.parse_args(argv)
    if not ns.command:
        parser.print_help()
        return 2

    try:
        if ns.command == "validate":
            report = validate_spec(load_spec(ns.spec), base_dir=Path(ns.spec).resolve().parent)
            if ns.json:
                payload = {k: v for k, v in report.items() if k != "normalized"}
                emit(payload, as_json=True)
            else:
                if report["ok"]:
                    print(f"OK: {ns.spec}")
                else:
                    print(f"INVALID: {ns.spec}")
                for err in report["errors"]:
                    print(f"  error: {err}")
                for warn in report["warnings"]:
                    print(f"  warning: {warn}")
                stats = report.get("stats", {})
                if stats:
                    print(f"  stats: {stats}")
            return 0 if report["ok"] else 1

        if ns.command == "explain":
            print(summary(load_spec(ns.spec)))
            return 0

        if ns.command == "normalize":
            report = validate_spec(load_spec(ns.spec), base_dir=Path(ns.spec).resolve().parent)
            if not report["ok"]:
                for err in report["errors"]:
                    print(f"error: {err}", file=sys.stderr)
                return 1
            Path(ns.out).parent.mkdir(parents=True, exist_ok=True)
            Path(ns.out).write_text(dumps(report["normalized"]) + "\n", encoding="utf-8")
            print(f"wrote {ns.out}")
            return 0

        if ns.command == "scaffold":
            params: dict[str, Any] = {}
            for item in ns.param:
                if "=" not in item:
                    print(f"error: --param must be KEY=VALUE, got {item!r}", file=sys.stderr)
                    return 2
                key, value = item.split("=", 1)
                params[key] = value
            spec = scaffold(ns.topology, params, ns.name)
            report = validate_spec(spec)
            if not report["ok"]:
                for err in report["errors"]:
                    print(f"error: {err}", file=sys.stderr)
                return 1
            Path(ns.out).parent.mkdir(parents=True, exist_ok=True)
            Path(ns.out).write_text(dumps(report["normalized"]) + "\n", encoding="utf-8")
            print(f"wrote {ns.out}")
            print(summary(report["normalized"]))
            for warn in report["warnings"]:
                print(f"warning: {warn}")
            return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
