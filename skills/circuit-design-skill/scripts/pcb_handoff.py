#!/usr/bin/env python3
"""Generate a handoff for the sibling ``pcb-design-skill``.

The PCB skill imports a SPICE netlist for connectivity and a JSON overlay for
board mechanics, footprints and rules.  This module writes that overlay plus
the two files the PCB skill needs to interpret a SPICE netlist correctly:

``<name>-pcb.json``          PCB design-spec overlay
``<name>-pinmap.json``       SPICE pin -> footprint pad map
``<name>-node-aliases.json`` SPICE node -> PCB net-name aliases
``<name>-pcb-handoff.md``    exact command, missing data and caveats

The handoff deliberately does **not** guess board dimensions, footprints or
package pinouts.  It writes what the circuit spec provides and reports what the
PCB skill still has to ask for.  Board dimensions are required; pass
``--pcb-board 50x40`` or set ``pcb.board`` in the circuit spec.

Usage::

    python3 scripts/pcb_handoff.py design.json --out out/
    python3 scripts/pcb_handoff.py design.json --out out/ --board 50x40
    python3 scripts/pcb_handoff.py design.json --out out/ \
        --board 50x40 --profile 2layer-low-speed --pcb-skill ../pcb-design-skill
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from circuit_common import dumps, spice_net, spice_number, spice_ref  # noqa: E402
from design_spec import (  # noqa: E402
    BOARDABLE_TYPES,
    SIMULATION_ONLY_TYPES,
    model_status,
    validate_spec,
)

DEFAULT_PROFILE = "2layer-low-speed"
DEFAULT_RULES: dict[str, float] = {
    "min_track_width_mm": 0.2,
    "min_clearance_mm": 0.2,
    "min_via_diameter_mm": 0.6,
    "min_via_drill_mm": 0.3,
    "min_hole_diameter_mm": 0.3,
    "min_annular_ring_mm": 0.15,
    "edge_clearance_mm": 0.5,
    "min_silk_width_mm": 0.15,
    "min_mask_sliver_mm": 0.1,
    "min_hole_to_hole_mm": 0.5,
}
DEFAULT_NETCLASSES: dict[str, dict[str, float]] = {
    "Default": {
        "track_width_mm": 0.25,
        "clearance_mm": 0.2,
        "via_diameter_mm": 0.6,
        "via_drill_mm": 0.3,
    },
    "Power": {
        "track_width_mm": 0.5,
        "clearance_mm": 0.3,
        "via_diameter_mm": 0.8,
        "via_drill_mm": 0.4,
    },
}
DEFAULT_BOARD: dict[str, Any] = {
    "thickness_mm": 1.6,
    "copper_oz": 1.0,
    "material": "FR4",
    "surface_finish": "HASL-LF",
    "mask_color": "green",
    "silkscreen_color": "white",
    "edge_radius_mm": 0.0,
}
_PIN_PREFIX = {"NPN": "Q", "PNP": "Q", "NMOS": "M", "PMOS": "M",
               "OPAMP": "X", "SW": "S"}
_OPAMP_PIN_ALIASES = {
    "in+": "inp", "in-": "inn", "v+": "vp", "v-": "vn",
    "out": "out", "inp": "inp", "inn": "inn", "vp": "vp", "vn": "vn",
    "noninverting": "inp", "inverting": "inn",
}
_DIODE_PIN_ALIASES = {
    "a": "a", "k": "k", "anode": "a", "cathode": "k",
    "1": "a", "2": "k",
}


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _pcb_name(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", str(name)).strip()
    if not safe:
        safe = "circuit"
    if not safe[0].isalnum():
        safe = "circuit-" + safe
    return safe[:64]


def _parse_board(text: str) -> tuple[float, float, int | None]:
    parts = [p for p in re.split(r"[xX,]", text.strip()) if p]
    if len(parts) not in (2, 3):
        raise ValueError("board must be WIDTHxHEIGHT or WIDTHxHEIGHTxLAYERS")
    try:
        width, height = float(parts[0]), float(parts[1])
        layers = int(parts[2]) if len(parts) == 3 else None
    except ValueError as exc:
        raise ValueError(f"invalid board dimensions {text!r}") from exc
    if width <= 0 or height <= 0:
        raise ValueError("board width and height must be > 0")
    if layers is not None and layers < 1:
        raise ValueError("board layers must be >= 1")
    return width, height, layers


def find_pcb_skill(explicit: str | Path | None = None) -> Path | None:
    """Locate the sibling skill without assuming a fixed installation path."""
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("PCB_DESIGN_SKILL")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "pcb-design-skill"
        if (candidate / "scripts" / "design_pipeline.py").is_file():
            return candidate
    return None


def _profile_data(profile: str, skill_dir: Path | None) -> dict[str, Any] | None:
    if skill_dir is None:
        return None
    path = skill_dir / "profiles" / f"{profile}.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    available = sorted(p.stem for p in (skill_dir / "profiles").glob("*.json"))
    raise ValueError(
        f"unknown PCB profile {profile!r}; available: {', '.join(available) or 'none'}"
    )


def _value_text(comp: dict[str, Any]) -> str:
    ctype = comp["type"]
    value = float(comp.get("value", 0.0) or 0.0)
    if ctype in ("R", "C", "L"):
        return spice_number(value)
    if ctype in ("D", "LED", "ZENER"):
        return spice_number(value) + "V" if value > 0 else comp.get("model", ctype)
    if ctype in ("NPN", "PNP", "NMOS", "PMOS"):
        return str(comp.get("model") or "generic " + ctype)
    if ctype == "OPAMP":
        return str(comp.get("model") or "generic op-amp")
    if ctype == "SW":
        return "ideal switch"
    return str(value)


def _estimate_current(spec: dict[str, Any]) -> float | None:
    params = spec.get("parameters") or {}
    for key in ("iout", "iout_max", "i_load", "iload", "load_current"):
        value = _as_float(params.get(key))
        if value is not None and value > 0:
            return value
    output = spec.get("output") or {}
    load = _as_float(output.get("load"))
    expected = _as_float(output.get("expected"))
    if load and expected and load > 0:
        return abs(expected) / load
    return None


def normalise_mapping(ctype: str, mapping: Any, ref: str) -> Any:
    if isinstance(mapping, (list, tuple)):
        pads = [str(pad).strip() for pad in mapping]
        if not pads or any(not pad for pad in pads):
            raise ValueError(f"{ref}: pin map pads must be non-empty")
        return pads
    if not isinstance(mapping, dict):
        raise ValueError(f"{ref}: pin map must be a list or object")

    keys = [str(key) for key in mapping]
    if keys and all(key.isdigit() for key in keys):
        ordered = [str(mapping[key]) for key in sorted(mapping, key=lambda k: int(str(k)))]
        if any(not pad.strip() for pad in ordered):
            raise ValueError(f"{ref}: pin map pads must be non-empty")
        return {str(key): str(mapping[key]) for key in sorted(
            mapping, key=lambda k: int(str(k)))}

    lowered = {str(key).strip().lower(): value for key, value in mapping.items()}
    if ctype == "OPAMP":
        translated: dict[str, Any] = {}
        for key, value in lowered.items():
            if key not in _OPAMP_PIN_ALIASES:
                raise ValueError(
                    f"{ref}: unknown op-amp pin {key!r}; expected IN+/IN-/V+/V-/OUT "
                    "or the subcircuit names inp/inn/vp/vn/out"
                )
            target = _OPAMP_PIN_ALIASES[key]
            if target in translated:
                raise ValueError(f"{ref}: pin map gives {target!r} more than once")
            translated[target] = value
        return translated
    if ctype in ("D", "LED", "ZENER"):
        translated = {}
        for key, value in lowered.items():
            if key not in _DIODE_PIN_ALIASES:
                raise ValueError(f"{ref}: unknown diode pin {key!r}; use a/k or 1/2")
            target = _DIODE_PIN_ALIASES[key]
            if target in translated:
                raise ValueError(f"{ref}: pin map gives {target!r} more than once")
            translated[target] = value
        return translated
    if ctype in ("NPN", "PNP", "NMOS", "PMOS"):
        allowed = {"c", "b", "e", "s"} if ctype in ("NPN", "PNP") else {"d", "g", "s", "b"}
        unknown = sorted(set(lowered) - allowed)
        if unknown:
            raise ValueError(
                f"{ref}: unknown pin name(s) {', '.join(unknown)}; "
                f"expected {', '.join(sorted(allowed))}"
            )
        return lowered
    if ctype == "SW":
        raise ValueError(
            f"{ref}: an ideal switch needs an ordered pad list "
            'such as ["1","2","3","4"], not pin names'
        )
    raise ValueError(f"{ref}: unsupported pin map for a {ctype} component")


def mapping_pad_nets(comp: dict[str, Any],
                     mapping: Any) -> list[tuple[str, str]]:
    """Expand a pin map into ``(pad, net)`` pairs.

    The SPICE MOSFET line carries a fourth bulk node.  That node is tied to
    the source net, so a three-pad package maps both ``s`` and ``b`` to the
    same pad.  Duplicate pads are therefore allowed when they carry the same
    net; a pad mapped to two different nets is an error.
    """
    pins = comp["pins"]
    ctype = comp["type"]
    pairs: list[tuple[str, str]] = []

    def pin_for_node(index: int) -> str | None:
        key = str(index)
        if key in pins:
            return key
        # The MOSFET SPICE line always has d g s b, but the spec has d/g/s.
        # The fourth (bulk) node is tied to the source net.
        if ctype in ("NMOS", "PMOS") and index == 4 and "3" in pins:
            return "3"
        return None

    def add(pad: Any, pin: str | None) -> None:
        if pin is None:
            return
        net = pins.get(pin)
        if net is None:
            return
        values = pad if isinstance(pad, (list, tuple)) else [pad]
        for value in values:
            pairs.append((str(value), str(net)))

    if isinstance(mapping, (list, tuple)):
        for index, pad in enumerate(mapping, start=1):
            add(pad, pin_for_node(index))
        return pairs
    if not isinstance(mapping, dict):
        return pairs

    keys = [str(key) for key in mapping]
    if keys and all(key.isdigit() for key in keys):
        for key in sorted(mapping, key=lambda item: int(str(item))):
            add(mapping[key], pin_for_node(int(str(key))))
        return pairs

    order = {
        "NPN": {"c": "1", "b": "2", "e": "3"},
        "PNP": {"c": "1", "b": "2", "e": "3"},
        "NMOS": {"d": "1", "g": "2", "s": "3", "b": "3"},
        "PMOS": {"d": "1", "g": "2", "s": "3", "b": "3"},
        "D": {"a": "1", "k": "2"},
        "LED": {"a": "1", "k": "2"},
        "ZENER": {"a": "1", "k": "2"},
        "OPAMP": {"inp": "1", "inn": "2", "vp": "3", "vn": "4", "out": "5"},
    }.get(ctype, {})
    for key, value in mapping.items():
        pin = order.get(str(key).lower())
        if pin is not None:
            add(value, pin)
    return pairs


def check_mapping_pads(comp: dict[str, Any], mapping: Any, ref: str) -> None:
    """Reject a pin map that shorts two different nets onto one pad."""
    pad_nets: dict[str, str] = {}
    for pad, net in mapping_pad_nets(comp, mapping):
        previous = pad_nets.get(pad)
        if previous is not None and previous != net:
            raise ValueError(
                f"{ref}: pad {pad} maps to both {previous!r} and {net!r}"
            )
        pad_nets[pad] = net


def build_pin_map(spec: dict[str, Any],
                  extra_refs: set[str] | None = None) -> tuple[dict[str, Any], list[str], list[str]]:
    """Return ``(pin_map, needs_pin_map, warnings)`` for the PCB skill."""
    extra_refs = extra_refs or set()
    pcb = spec.get("pcb") or {}
    raw = pcb.get("pin_map") or {}
    warnings: list[str] = []
    if not isinstance(raw, dict):
        raise ValueError("pcb.pin_map must be an object")
    if set(raw) & {"refs", "defaults", "include", "exclude"}:
        refs = copy.deepcopy(raw.get("refs") or {})
        defaults = copy.deepcopy(raw.get("defaults") or {})
        include = [str(ref) for ref in (raw.get("include") or [])]
        exclude = [str(ref) for ref in (raw.get("exclude") or [])]
    else:
        refs = copy.deepcopy(raw)
        defaults = {}
        include = []
        exclude = []

    by_ref = {comp["ref"]: comp for comp in spec["components"]}
    by_spice = {spice_ref(comp["ref"], comp["type"]): comp
                for comp in spec["components"]}

    def resolve(ref: str) -> dict[str, Any] | None:
        return by_ref.get(ref) or by_spice.get(ref)

    kept_include: list[str] = []
    for ref in include:
        comp = resolve(ref)
        if comp is None:
            warnings.append(f"pcb.pin_map.include references unknown component {ref!r}")
            continue
        if comp["type"] in SIMULATION_ONLY_TYPES:
            warnings.append(
                f"{ref} is a simulation-only {comp['type']} source and cannot be "
                "placed on the board; use pcb.extra_components for a connector"
            )
            continue
        kept_include.append(spice_ref(comp["ref"], comp["type"]))
    include = kept_include
    include_set = set(include)
    normalised_refs: dict[str, Any] = {}
    for ref in refs:
        comp = resolve(ref)
        if comp is None:
            warnings.append(f"pcb.pin_map.refs references unknown component {ref!r}")
            continue
        element = spice_ref(comp["ref"], comp["type"])
        if comp["type"] in SIMULATION_ONLY_TYPES:
            warnings.append(
                f"{ref} is simulation-only and was removed from the pin map"
            )
            continue
        if comp["type"] == "SW" and element not in include_set:
            warnings.append(
                f"{ref} is an ideal switch and is excluded from the board; "
                "model the real transistor as NMOS/PMOS"
            )
            continue
        if element in normalised_refs:
            raise ValueError(f"{ref}: more than one pin map entry resolves to {element}")
        normalised = normalise_mapping(comp["type"], refs[ref], element)
        check_mapping_pads(comp, normalised, ref)
        normalised_refs[element] = normalised
    refs = normalised_refs

    # KiCad's Device:D and Device:LED symbols number pin 1 = K (cathode) and
    # pin 2 = A (anode). SPICE diode nodes are (anode, cathode), so the default
    # footprint pad order is 2,1 -- not 1,2.
    normalised_defaults: dict[str, Any] = {
        "R": ["1", "2"], "C": ["1", "2"], "L": ["1", "2"], "D": ["2", "1"],
    }
    default_type = {"R": "R", "C": "C", "L": "L", "D": "D",
                    "Q": "NPN", "M": "NMOS", "J": "NMOS",
                    "X": "OPAMP", "S": "SW"}
    for prefix, mapping in defaults.items():
        key = str(prefix).upper()
        normalised_defaults[key] = normalise_mapping(
            default_type.get(key, key), mapping, f"defaults.{prefix}", warnings,
        )

    excluded: list[str] = []
    for ref in exclude:
        comp = resolve(str(ref))
        if comp is None:
            warnings.append(
                f"pcb.pin_map.exclude references unknown component {ref!r}"
            )
            element = str(ref)
        else:
            element = spice_ref(comp["ref"], comp["type"])
            if (comp["type"] not in SIMULATION_ONLY_TYPES
                    and comp["type"] != "SW"):
                warnings.append(
                    f"{comp['ref']} is a boardable part but was explicitly "
                    "excluded; the PCB will not contain it"
                )
        if element not in excluded:
            excluded.append(element)
    for comp in spec["components"]:
        ref, ctype = comp["ref"], comp["type"]
        element = spice_ref(ref, ctype)
        if ctype in SIMULATION_ONLY_TYPES:
            if element not in excluded:
                excluded.append(element)
        elif ctype == "SW" and element not in include_set:
            if element not in excluded:
                excluded.append(element)
    for ref in sorted(extra_refs):
        if ref not in excluded:
            excluded.append(ref)
    for element in include_set:
        if element in excluded:
            warnings.append(f"{element} is both included and excluded; include wins")
            excluded.remove(element)

    needs_pin_map: list[str] = []
    for comp in spec["components"]:
        ref, ctype = comp["ref"], comp["type"]
        element = spice_ref(ref, ctype)
        if ctype in SIMULATION_ONLY_TYPES:
            continue
        if ctype == "SW" and element not in include_set:
            continue
        prefix = _PIN_PREFIX.get(ctype)
        if prefix is None:
            continue
        if element in refs or prefix in normalised_defaults:
            continue
        needs_pin_map.append(ref)

    return (
        {
            "defaults": normalised_defaults,
            "refs": refs,
            "include": include,
            "exclude": excluded,
        },
        needs_pin_map,
        warnings,
    )


def build_node_aliases(spec: dict[str, Any],
                       extra_nets: set[str] | None = None) -> dict[str, str]:
    """Return SPICE node -> circuit net-name aliases for the PCB importer."""
    extra_nets = extra_nets or set()
    pcb = spec.get("pcb") or {}
    aliases: dict[str, str] = {}
    raw_aliases = pcb.get("node_aliases") or {}
    if not isinstance(raw_aliases, dict):
        raise ValueError("pcb.node_aliases must be an object")
    for node, net_name in raw_aliases.items():
        if not isinstance(node, str) or not node:
            raise ValueError("pcb.node_aliases keys must be non-empty strings")
        if not isinstance(net_name, str) or not net_name:
            raise ValueError(f"pcb.node_aliases[{node!r}] must be a non-empty string")
        if any(ch.isspace() for ch in net_name):
            raise ValueError(f"pcb.node_aliases[{node!r}] must not contain whitespace")
        aliases[node] = net_name

    ground = spec["ground"]
    aliases["0"] = "0" if ground == "0" else ground

    nets = set(extra_nets)
    for comp in spec["components"]:
        nets.update(str(net) for net in comp["pins"].values())
    for net in sorted(nets):
        if any(ch.isspace() for ch in net):
            raise ValueError(f"net {net!r} contains whitespace; rename it")
        node = spice_net(net, ground)
        if node == net:
            continue
        previous = aliases.get(node)
        if previous is not None and previous != net:
            raise ValueError(
                f"SPICE node {node!r} maps to both {previous!r} and {net!r}; "
                "rename one of the nets"
            )
        aliases[node] = net
    return aliases


def _board(spec: dict[str, Any], board_arg: str | None,
           layers_arg: int | None) -> tuple[dict[str, Any], list[str]]:
    pcb = spec.get("pcb") or {}
    warnings: list[str] = []
    board = copy.deepcopy(pcb.get("board") or {})
    if not isinstance(board, dict):
        raise ValueError("pcb.board must be an object")
    if board_arg:
        width, height, layers = _parse_board(board_arg)
        board["width_mm"] = width
        board["height_mm"] = height
        if layers is not None:
            board["layers"] = layers
    if layers_arg is not None:
        board["layers"] = layers_arg
    if "width_mm" not in board or "height_mm" not in board:
        raise ValueError(
            "pcb.board.width_mm and height_mm are required; set them in the "
            "circuit spec or pass --pcb-board WIDTHxHEIGHT "
            "(--board for pcb_handoff.py)"
        )
    for key, value in DEFAULT_BOARD.items():
        board.setdefault(key, value)
    board.setdefault("layers", 2)
    for key in ("width_mm", "height_mm", "thickness_mm", "copper_oz"):
        value = _as_float(board.get(key))
        if value is None or value <= 0:
            raise ValueError(f"pcb.board.{key} must be > 0")
        board[key] = value
    layers = board.get("layers")
    if not isinstance(layers, int) or isinstance(layers, bool) or layers < 1:
        raise ValueError("pcb.board.layers must be a positive integer")
    return board, warnings


def _rules(spec: dict[str, Any]) -> dict[str, Any]:
    pcb = spec.get("pcb") or {}
    rules = copy.deepcopy(pcb.get("rules") or {})
    if not isinstance(rules, dict):
        raise ValueError("pcb.rules must be an object")
    for key, value in DEFAULT_RULES.items():
        rules.setdefault(key, value)
    return rules


def _netclasses(spec: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    pcb = spec.get("pcb") or {}
    warnings: list[str] = []
    classes = copy.deepcopy(DEFAULT_NETCLASSES)
    user = pcb.get("netclasses") or {}
    if not isinstance(user, dict):
        raise ValueError("pcb.netclasses must be an object")
    for name, values in user.items():
        if not isinstance(values, dict):
            raise ValueError(f"pcb.netclasses.{name} must be an object")
        base = copy.deepcopy(classes.get(name, classes["Default"]))
        base.update(values)
        classes[name] = base
    if "Default" not in classes:
        classes["Default"] = copy.deepcopy(DEFAULT_NETCLASSES["Default"])

    requested = {str(net.get("class", "Default")) for net in (pcb.get("nets") or [])
                 if isinstance(net, dict)}
    for name in sorted(requested - set(classes)):
        warnings.append(
            f"netclass {name!r} is not defined; using Default geometry for it"
        )
        classes[name] = copy.deepcopy(classes["Default"])
    return classes, warnings


def _nets(spec: dict[str, Any], components: list[dict[str, Any]],
          extra: list[dict[str, Any]], netclasses: dict[str, Any],
          aliases: dict[str, str]) -> list[dict[str, Any]]:
    pcb = spec.get("pcb") or {}
    ground = spec["ground"]
    supplies = {
        str(item.get("name")): _as_float(item.get("voltage"))
        for item in (spec.get("supplies") or [])
        if isinstance(item, dict) and item.get("name")
    }
    current = _estimate_current(spec)
    ordered: dict[str, dict[str, Any]] = {}

    def add(name: str, **fields: Any) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("net names must be non-empty strings")
        if any(ch.isspace() for ch in name):
            raise ValueError(f"net {name!r} contains whitespace")
        entry = ordered.setdefault(name, {"name": name})
        for key, value in fields.items():
            if value is not None:
                entry[key] = value

    add(ground, **{"class": "Power", "voltage_v": 0.0,
                   **({"current_a": current} if current else {})})
    for name, voltage in supplies.items():
        fields: dict[str, Any] = {"class": "Power"}
        if voltage is not None:
            fields["voltage_v"] = abs(voltage)
            if voltage < 0:
                fields["notes"] = f"negative rail {voltage:g} V"
        if current:
            fields["current_a"] = current
        add(name, **fields)

    for comp in components:
        for net in comp["pins"].values():
            if net not in ordered:
                add(str(net), **{"class": "Default"})
    for comp in extra:
        for net in (comp.get("pads") or {}).values():
            if net not in ordered:
                add(str(net), **{"class": "Default"})

    for item in pcb.get("nets") or []:
        if not isinstance(item, dict) or "name" not in item:
            raise ValueError("each pcb.nets entry needs a name")
        name = str(item["name"])
        fields = {key: item[key] for key in
                  ("class", "voltage_v", "current_a", "impedance_ohm",
                   "diff_pair", "notes") if key in item}
        add(name, **fields)

    # If an alias renames a SPICE node to a net that is not in the list, add it
    # so the overlay and the imported netlist agree.
    for net_name in aliases.values():
        if net_name not in ordered:
            add(net_name, **{"class": "Default"})

    for entry in ordered.values():
        if entry.get("class") not in netclasses:
            entry["class"] = "Default"
    return list(ordered.values())


def _component_entries(spec: dict[str, Any], include_sw: set[str]
                       ) -> tuple[list[dict[str, Any]], list[str]]:
    pcb = spec.get("pcb") or {}
    footprints = pcb.get("footprints") or {}
    if not isinstance(footprints, dict):
        raise ValueError("pcb.footprints must be an object")
    placement = pcb.get("placement") or {}
    at_by_ref = {}
    if isinstance(placement, dict):
        at_by_ref = placement.get("components") or {}
        if not isinstance(at_by_ref, dict):
            raise ValueError("pcb.placement.components must be an object")
    warnings: list[str] = []
    entries: list[dict[str, Any]] = []
    for comp in spec["components"]:
        ref, ctype = comp["ref"], comp["type"]
        if ctype in SIMULATION_ONLY_TYPES:
            warnings.append(
                f"{ref} ({ctype}) is simulation-only and is not placed on the board"
            )
            continue
        if ctype == "SW" and ref not in include_sw:
            warnings.append(
                f"{ref} is an ideal switch; it is not placed on the board. "
                "Use a real NMOS/PMOS plus a gate driver for the PCB"
            )
            continue
        if ctype not in BOARDABLE_TYPES and ctype != "SW":
            warnings.append(f"{ref} ({ctype}) is not a boardable component")
            continue
        element = spice_ref(ref, ctype)
        entry: dict[str, Any] = {
            "ref": element,
            "value": _value_text(comp),
            "footprint": str(comp.get("footprint") or footprints.get(ref)
                             or footprints.get(element) or ""),
            "dnp": bool(comp.get("dnp", False)),
            "properties": {
                "circuit_ref": ref,
                "spice_type": ctype,
                "logical_pins": json.dumps(comp["pins"], sort_keys=True,
                                           separators=(",", ":")),
            },
        }
        at = at_by_ref.get(ref)
        if at is None:
            at = at_by_ref.get(element)
        if at is not None:
            if not isinstance(at, dict) or "x_mm" not in at or "y_mm" not in at:
                raise ValueError(
                    f"pcb.placement.components.{ref} needs x_mm and y_mm"
                )
            entry["at"] = {
                "x_mm": float(at["x_mm"]),
                "y_mm": float(at["y_mm"]),
                "rotation_deg": float(at.get("rotation_deg", 0)),
                "side": str(at.get("side", "front")),
            }
        entries.append(entry)
    return entries, warnings


def _extra_components(spec: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    pcb = spec.get("pcb") or {}
    raw = pcb.get("extra_components") or []
    if not isinstance(raw, list):
        raise ValueError("pcb.extra_components must be a list")
    warnings: list[str] = []
    allowed = {"ref", "value", "footprint", "at", "pads", "dnp", "properties", "board_only"}
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each pcb.extra_components entry must be an object")
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise ValueError(
                f"pcb.extra_components has unknown key(s): {', '.join(unknown)}"
            )
        ref = item.get("ref")
        if not isinstance(ref, str) or not ref:
            raise ValueError("pcb.extra_components entries need a non-empty ref")
        entry = {
            "ref": ref,
            "value": str(item.get("value", "")),
            "footprint": str(item.get("footprint", "")),
            "dnp": bool(item.get("dnp", False)),
            "board_only": bool(item.get("board_only", False)),
        }
        pads = item.get("pads")
        if not isinstance(pads, dict):
            raise ValueError(f"pcb.extra_components {ref} needs a pads object")
        # An empty pads object is valid for mechanical parts such as mounting
        # holes, which have no electrical connection.
        entry["pads"] = {str(pad): str(net) for pad, net in pads.items()}
        if "at" in item:
            at = item["at"]
            if not isinstance(at, dict) or "x_mm" not in at or "y_mm" not in at:
                raise ValueError(f"pcb.extra_components {ref}.at needs x_mm and y_mm")
            entry["at"] = {
                "x_mm": float(at["x_mm"]),
                "y_mm": float(at["y_mm"]),
                "rotation_deg": float(at.get("rotation_deg", 0)),
                "side": str(at.get("side", "front")),
            }
        if "properties" in item:
            if not isinstance(item["properties"], dict):
                raise ValueError(f"pcb.extra_components {ref}.properties must be an object")
            entry["properties"] = {str(k): str(v) for k, v in item["properties"].items()}
        result.append(entry)
    return result, warnings


def build_overlay(spec: dict[str, Any], *, board: dict[str, Any], profile: str,
                  pin_map: dict[str, Any], aliases: dict[str, str],
                  skill_dir: Path | None,
                  base_dir: str | Path | None = None) -> tuple[dict[str, Any], list[str]]:
    pcb = spec.get("pcb") or {}
    warnings: list[str] = []
    include_sw = set(pin_map.get("include") or [])
    exclude_elements = set(pin_map.get("exclude") or [])
    components, comp_warnings = _component_entries(spec, include_sw)
    warnings.extend(comp_warnings)
    # An explicit pin-map exclude means the part is not on the board. Keep the
    # overlay consistent with the pin map instead of listing it with no pads.
    components = [comp for comp in components
                  if comp["ref"] not in exclude_elements]
    extra, extra_warnings = _extra_components(spec)
    warnings.extend(extra_warnings)
    by_ref = {c["ref"]: c for c in components + extra}
    if len(by_ref) != len(components) + len(extra):
        raise ValueError("duplicate component reference in the PCB overlay")

    netclasses, nc_warnings = _netclasses(spec)
    warnings.extend(nc_warnings)
    electrical = [
        comp for comp in spec["components"]
        if (comp["type"] in BOARDABLE_TYPES
            or (comp["type"] == "SW"
                and spice_ref(comp["ref"], comp["type"]) in include_sw))
        and spice_ref(comp["ref"], comp["type"]) not in exclude_elements
    ]
    nets = _nets(spec, electrical, extra, netclasses, aliases)

    placement_raw = copy.deepcopy(pcb.get("placement") or {})
    if not isinstance(placement_raw, dict):
        raise ValueError("pcb.placement must be an object")
    placement_raw.pop("components", None)
    placement_raw.setdefault("mode", "auto")
    if placement_raw["mode"] not in ("auto", "fixed"):
        raise ValueError("pcb.placement.mode must be 'auto' or 'fixed'")
    if placement_raw["mode"] == "fixed":
        missing = [c["ref"] for c in components if "at" not in c]
        if missing:
            warnings.append(
                "pcb.placement.mode is fixed but these parts have no at "
                "coordinates: " + ", ".join(missing)
            )

    routing = copy.deepcopy(pcb.get("routing") or {})
    if not isinstance(routing, dict):
        raise ValueError("pcb.routing must be an object")
    routing.setdefault("mode", "freerouting")
    if routing["mode"] not in ("freerouting", "none"):
        raise ValueError("pcb.routing.mode must be 'freerouting' or 'none'")

    assumptions = [str(a) for a in (pcb.get("assumptions") or [])]
    assumptions += [
        "The PCB overlay is generated from the circuit spec; the SPICE netlist "
        "is the electrical source of truth.",
        "Logical SPICE pin numbers are not physical footprint pads; pin maps "
        "must come from the real package datasheet.",
        "The pcb-design-skill DRC, DFM and unconnected gates, not this handoff, "
        "decide whether the board is manufacturable.",
    ]
    if any(c["type"] in ("SW",) for c in spec["components"]):
        assumptions.append(
            "Ideal switches and simulation sources are excluded from the board."
        )
    status = model_status(spec, base_dir=base_dir)
    if status.startswith("unverified"):
        assumptions.append(f"Semiconductor models are {status}.")

    override = {
        "version": 1,
        "name": _pcb_name(spec["name"]),
        "profile": profile,
        "title": str(pcb.get("title") or spec.get("description") or spec["name"]),
        "revision": str(pcb.get("revision") or "A"),
        "board": board,
        "rules": _rules(spec),
        "netclasses": netclasses,
        "nets": nets,
        "components": components + extra,
        "placement": placement_raw,
        "routing": routing,
        "assumptions": assumptions,
        "netlist_source": f"{spec['name']}.cir",
    }
    for key in ("zones", "fabrication", "signoff"):
        if key in pcb:
            override[key] = copy.deepcopy(pcb[key])
    return override, warnings


def _handoff_markdown(spec: dict[str, Any], summary: dict[str, Any]) -> str:
    name = spec["name"]
    board = summary["board"]
    lines = [
        f"# PCB handoff: {name}",
        "",
        "Generated by `circuit-design-skill` for the sibling `pcb-design-skill`.",
        "",
        "> The SPICE netlist is the electrical source of truth. The JSON overlay",
        "> supplies board mechanics, footprints and rules. The PCB skill's DRC,",
        "> DFM and unconnected gates decide whether the board is manufacturable.",
        "",
        "## Status",
        "",
        f"- Ready for non-interactive `--no-ask`: **{'yes' if summary['ready'] else 'no'}**",
        f"- Board: {board['width_mm']:g} x {board['height_mm']:g} mm, "
        f"{board['layers']} layer(s)",
        f"- Profile: `{summary['profile']}`",
        f"- Simulation status: **{summary['simulation_status']}**",
        "",
    ]
    ref_map = summary.get("ref_map") or {}

    def label(ref: str) -> str:
        spice = ref_map.get(ref)
        return f"`{ref}`" + (f" (SPICE `{spice}`)" if spice else "")

    if summary["missing_footprints"]:
        lines += [
            "### Missing footprints",
            "",
            "The PCB pipeline will ask for these. Add them under `pcb.footprints` "
            "or per-component `footprint` for a non-interactive run.",
            "",
            *[f"- {label(ref)}" for ref in summary["missing_footprints"]],
            "",
        ]
    if summary["needs_pin_map"]:
        lines += [
            "### Pin maps the PCB skill will ask for",
            "",
            "Add a `pcb.pin_map.refs` entry from the real package datasheet. "
            "Do **not** assume logical pin 1 is package pin 1.",
            "",
            *[f"- {label(ref)}" for ref in summary["needs_pin_map"]],
            "",
        ]
    if summary["excluded"]:
        lines += [
            "### Excluded parts",
            "",
            *[f"- `{ref}`" for ref in summary["excluded"]],
            "",
            "Voltage/current sources, controlled sources and ideal switches are",
            "not physical parts. If a real part appears in this list, it was",
            "explicitly excluded and will be missing from the board. Add",
            "connectors and real transistors with `pcb.extra_components` or a",
            "revised circuit spec before layout.",
            "",
        ]
    if summary.get("extra_components"):
        lines += [
            "### Extra board-only components",
            "",
            *[f"- `{ref}`" for ref in summary["extra_components"]],
            "",
            "These are in the overlay but not in the SPICE netlist; they are",
            "listed in the pin map's `exclude` so the PCB importer keeps them.",
            "",
        ]
    if ref_map:
        lines += [
            "### Reference mapping",
            "",
            "SPICE subcircuit instances must start with `X`. The schematic/BOM",
            "keeps the circuit reference; the PCB netlist and overlay use the",
            "SPICE element name:",
            "",
            *[f"- `{ref}` -> `{spice}`" for ref, spice in sorted(ref_map.items())],
            "",
        ]

    lines += [
        "## Command",
        "",
        summary["run_hint"],
        "",
        "```bash",
        summary["command"],
        "```",
        "",
    ]
    if summary["ready"]:
        lines += [
            "Non-interactive form:",
            "",
            "```bash",
            summary["command_no_ask"],
            "```",
            "",
        ]
    else:
        lines += [
            "The command above intentionally omits `--no-ask`, so the PCB skill",
            "can ask for the missing footprints, pin maps or placement.",
            "",
        ]
    lines += [
        "## Files",
        "",
        f"- `{summary['files']['overlay']}` - PCB design-spec overlay",
        f"- `{summary['files']['pin_map']}` - SPICE pin -> footprint pad map",
        f"- `{summary['files']['node_aliases']}` - SPICE node -> PCB net aliases",
        f"- `{summary['files']['netlist']}` - SPICE connectivity source",
        "",
        "## Before layout",
        "",
        "- Replace ideal sources and switches with real connectors, transistors",
        "  and gate drivers. A simulation source is not a board component.",
        "- Check every footprint's pads against the package datasheet. The KiCad",
        "  footprint name is not proof that the pad numbering matches the pin map.",
        "- Add decoupling, bulk capacitors, connectors, test points, mounting",
        "  holes and any mechanical parts the circuit spec does not model.",
        "- Check power/current, thermal, creepage/clearance and safety rules for",
        "  the target profile; the default rules are a starting point, not a",
        "  manufacturer guarantee.",
        "- Run ERC, DRC, unconnected and DFM gates in `pcb-design-skill`.",
        "",
    ]
    if summary["warnings"]:
        lines += ["## Warnings", ""]
        lines += [f"- {warning}" for warning in summary["warnings"]]
        lines.append("")
    return "\n".join(lines)


def write_handoff(spec: dict[str, Any], outdir: str | Path,
                  *, board: str | None = None, profile: str | None = None,
                  layers: int | None = None,
                  pcb_skill: str | Path | None = None,
                  base_dir: str | Path | None = None) -> dict[str, Any]:
    """Write the PCB overlay, pin map, node aliases and handoff document."""
    report = validate_spec(spec, base_dir=base_dir)
    if not report["ok"]:
        raise ValueError(
            "invalid design spec: " + "; ".join(report["errors"])
        )
    spec = report["normalized"]
    pcb = spec.get("pcb") or {}
    if pcb.get("enabled") is False:
        return {"enabled": False, "files": {}, "ready": False,
                "warnings": ["pcb.enabled is false; no handoff written"]}

    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    name = spec["name"]
    # The PCB skill imports connectivity from this exact file.  Write it here
    # too so the standalone handoff command is self-contained.  The import is
    # lazy to avoid a circular import when generate.py calls this module.
    from generate import spice_netlist  # noqa: PLC0415

    (out / f"{name}.cir").write_text(
        spice_netlist(spec).rstrip() + "\n", encoding="utf-8"
    )
    skill_dir = find_pcb_skill(pcb_skill)

    board_data, board_warnings = _board(spec, board, layers)
    profile_name = str(profile or pcb.get("profile") or "")
    if not profile_name:
        if board_data["layers"] == 2:
            profile_name = DEFAULT_PROFILE
        else:
            raise ValueError(
                "pcb.profile is required for a board with more than 2 layers; "
                "choose one from pcb-design-skill/profiles"
            )
    profile_info = _profile_data(profile_name, skill_dir)
    if profile_info:
        layer_range = profile_info.get("layers") or {}
        if "min" in layer_range and board_data["layers"] < int(layer_range["min"]):
            raise ValueError(
                f"profile {profile_name!r} requires at least {layer_range['min']} layers"
            )
        if "max" in layer_range and board_data["layers"] > int(layer_range["max"]):
            raise ValueError(
                f"profile {profile_name!r} allows at most {layer_range['max']} layers"
            )

    extra, extra_warnings = _extra_components(spec)
    extra_refs = {comp["ref"] for comp in extra}
    pin_map, needs_pin_map, pin_warnings = build_pin_map(spec, extra_refs)
    aliases = build_node_aliases(spec, {
        str(net) for comp in extra for net in comp.get("pads", {}).values()
    })
    overlay, overlay_warnings = build_overlay(
        spec, board=board_data, profile=profile_name, pin_map=pin_map,
        aliases=aliases, skill_dir=skill_dir, base_dir=base_dir,
    )

    warnings = list(report.get("warnings", []))
    for warning in (board_warnings + extra_warnings + pin_warnings
                    + overlay_warnings):
        if warning not in warnings:
            warnings.append(warning)
    def circuit_ref(comp: dict[str, Any]) -> str:
        return str((comp.get("properties") or {}).get("circuit_ref") or comp["ref"])

    missing_footprints = [
        circuit_ref(comp) for comp in overlay["components"]
        if not str(comp.get("footprint") or "").strip()
    ]
    placement = overlay.get("placement") or {}
    missing_placement = []
    if placement.get("mode") == "fixed":
        missing_placement = [
            circuit_ref(comp) for comp in overlay["components"] if "at" not in comp
        ]
    ref_map = {
        comp["ref"]: spice_ref(comp["ref"], comp["type"])
        for comp in spec["components"]
        if comp["ref"] != spice_ref(comp["ref"], comp["type"])
    }
    ready = not missing_footprints and not needs_pin_map and not missing_placement

    files = {
        "overlay": f"{name}-pcb.json",
        "pin_map": f"{name}-pinmap.json",
        "node_aliases": f"{name}-node-aliases.json",
        "handoff": f"{name}-pcb-handoff.md",
        "netlist": f"{name}.cir",
    }
    (out / files["overlay"]).write_text(dumps(overlay) + "\n", encoding="utf-8")
    (out / files["pin_map"]).write_text(dumps(pin_map) + "\n", encoding="utf-8")
    (out / files["node_aliases"]).write_text(dumps(aliases) + "\n", encoding="utf-8")

    overlay_path = (out / files["overlay"]).resolve()
    netlist_path = (out / files["netlist"]).resolve()
    pinmap_path = (out / files["pin_map"]).resolve()
    aliases_path = (out / files["node_aliases"]).resolve()
    if skill_dir:
        pipeline_text = str(
            (skill_dir / "scripts" / "design_pipeline.py").resolve()
        )
        run_hint = "Run this from anywhere; the paths are absolute."
    else:
        pipeline_text = "/path/to/pcb-design-skill/scripts/design_pipeline.py"
        run_hint = (
            "Replace the placeholder path with the installed pcb-design-skill."
        )
    command = (
        f"python3 {shlex.quote(pipeline_text)} all "
        f"{shlex.quote(str(overlay_path))} "
        f"--ltspice {shlex.quote(str(netlist_path))} "
        f"--pin-map {shlex.quote(str(pinmap_path))} "
        f"--node-aliases {shlex.quote(str(aliases_path))}"
    )
    command_no_ask = command + " --no-ask"

    summary = {
        "enabled": True,
        "ready": ready,
        "name": overlay["name"],
        "profile": profile_name,
        "board": board_data,
        "files": files,
        "missing_footprints": missing_footprints,
        "needs_pin_map": needs_pin_map,
        "missing_placement": missing_placement,
        "excluded": [ref for ref in pin_map.get("exclude", [])
                     if ref not in extra_refs],
        "extra_components": sorted(extra_refs),
        "ref_map": ref_map,
        "warnings": warnings,
        "command": command,
        "command_no_ask": command_no_ask,
        "run_hint": run_hint,
        "simulation_status": model_status(spec, base_dir=base_dir),
    }
    (out / files["handoff"]).write_text(
        _handoff_markdown(spec, summary), encoding="utf-8"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pcb_handoff.py",
        description="Write pcb-design-skill overlay, pin map and node aliases.",
    )
    parser.add_argument("spec")
    parser.add_argument("--out", required=True)
    parser.add_argument("--board", metavar="WxH[xL]",
                        help="board size in mm, e.g. 50x40 or 50x40x2")
    parser.add_argument("--layers", type=int)
    parser.add_argument("--profile", help="PCB skill profile name")
    parser.add_argument("--pcb-skill", help="path to the pcb-design-skill directory")
    ns = parser.parse_args(argv)
    try:
        raw = json.loads(Path(ns.spec).read_text(encoding="utf-8"))
        report = validate_spec(raw, base_dir=Path(ns.spec).resolve().parent)
        if not report["ok"]:
            print("error: invalid design spec: " + "; ".join(report["errors"]),
                  file=sys.stderr)
            return 1
        spec = report["normalized"]
        summary = write_handoff(
            spec, ns.out, board=ns.board, profile=ns.profile,
            layers=ns.layers, pcb_skill=ns.pcb_skill,
            base_dir=Path(ns.spec).resolve().parent,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not summary.get("enabled", True):
        print("pcb handoff disabled: " + "; ".join(summary.get("warnings", [])))
        return 0
    for kind, path in summary["files"].items():
        print(f"wrote {kind}: {Path(ns.out) / path}")
    print(f"ready for --no-ask: {'yes' if summary['ready'] else 'no'}")
    if summary["missing_footprints"]:
        print("missing footprints: " + ", ".join(summary["missing_footprints"]))
    if summary["needs_pin_map"]:
        print("needs pin map: " + ", ".join(summary["needs_pin_map"]))
    for warning in summary["warnings"]:
        print(f"warning: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
