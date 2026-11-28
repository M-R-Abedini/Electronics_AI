#!/usr/bin/env python3
"""Verify a generated design and record evidence.

Verification is layered, and every layer says exactly what it proved:

1. **Spec validity** - schema, pin and net checks.
2. **Netlist agreement** - the generated SPICE, LTspice and KiCad files are
   re-read and compared with the component list.  This catches the classic
   failure where a schematic *looks* right but netlists to different nodes.
3. **Linear electrical check** - an independent pure-Python MNA solve of the
   linear part, compared with closed-form expectations (divider ratio, RC and
   Sallen-Key corner, op-amp gain).
4. **ngspice** - if installed *and compatible*, the generated netlist is
   simulated.  Without ngspice, nonlinear circuits are reported as *not
   simulated*, never as passing.
5. **Metrics** - gain, bandwidth, THD, noise and RMS level are extracted from
   the ngspice rawfile and compared with the declared ``metrics`` targets.
   Missing ngspice is a skip; a declared metric that cannot be evaluated is a
   failure, never a silent pass.
6. **KiCad ERC** - with ``--erc``, KiCad's own electrical rules run when
   ``kicad-cli`` is installed and compatible.  Missing tools are a skip, not a
   pass; ``--require`` turns a skip in a required stage into a failure.
7. **Provenance** - the result records whether it came from this machine or
   from CI, plus the commit and run id when running in GitHub Actions.

Generic device models are placeholders.  A structural pass is not a promise
that a real transistor or diode will behave the same way.

Usage::

    python3 verify.py spec.json --out out/
    python3 verify.py spec.json --out out/ --json
    python3 verify.py spec.json --out out/ --no-update-report
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import env_probe  # noqa: E402
import generate as gen  # noqa: E402
import metrics as metric_lib  # noqa: E402
import ngspice_raw  # noqa: E402
from circuit_common import dumps, si_number, spice_number, spice_ref  # noqa: E402
from design_spec import (  # noqa: E402
    BOARDABLE_TYPES,
    SIMULATION_ONLY_TYPES,
    canonical_pin,
    model_records,
    model_status,
    resolve_model,
    validate_spec,
)
from mna import MNAError, UnsupportedElement, from_components  # noqa: E402
from symbols import asc_pins  # noqa: E402

LINEAR_TYPES = {"R", "C", "L", "V", "I", "E", "G", "OPAMP"}

# Stage name used by env_probe.py -> the check it must turn into a PASS.
REQUIRED_CHECK = {
    "simulate": "ngspice",
    "erc": "kicad_erc",
    "pcb-drc": "kicad_pcb_drc",
    "kicad-parse": "kicad_schematic",
    "models": "models",
    "design": "design",
    "measurements": "measurement_plan",
    "limits": "operating_limits",
}


def provenance(required: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
    """Record where the evidence came from, so CI cannot be mistaken for local."""
    backend = os.environ.get("CIRCUIT_BACKEND") or (
        "ci" if os.environ.get("GITHUB_ACTIONS") else "local")
    data: dict[str, Any] = {"backend": backend,
                            "required_stages": list(required or [])}
    if os.environ.get("GITHUB_ACTIONS"):
        data.update({
            "commit": os.environ.get("GITHUB_SHA"),
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "repository": os.environ.get("GITHUB_REPOSITORY"),
        })
    return data


class Check:
    def __init__(self, name: str, ok: bool | None, detail: str, evidence: Any = None):
        self.name = name
        self.ok = ok
        self.detail = detail
        self.evidence = evidence

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail,
                "evidence": self.evidence}


# ---------------------------------------------------------------------------
# Tiny S-expression parser (for KiCad netlists)
# ---------------------------------------------------------------------------

def parse_sexp(text: str) -> Any:
    tokens = re.findall(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+', text)
    pos = 0

    def read() -> Any:
        nonlocal pos
        if pos >= len(tokens):
            raise ValueError("unexpected end of s-expression")
        token = tokens[pos]
        pos += 1
        if token == "(":
            items = []
            while pos < len(tokens) and tokens[pos] != ")":
                items.append(read())
            if pos >= len(tokens):
                raise ValueError("missing closing parenthesis")
            pos += 1
            return items
        if token == ")":
            raise ValueError("unexpected closing parenthesis")
        if token.startswith('"'):
            return token[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        return token

    value = read()
    if pos != len(tokens):
        raise ValueError("trailing tokens after s-expression")
    return value


def sexp_find(node: Any, key: str) -> list[Any]:
    if not isinstance(node, list):
        return []
    return [item for item in node if isinstance(item, list) and item and item[0] == key]


def sexp_children(node: list, key: str) -> list[Any]:
    return sexp_find(node, key)


# ---------------------------------------------------------------------------
# Structural checks
# ---------------------------------------------------------------------------

def check_spice(spec: dict[str, Any], path: Path,
                base_dir: str | Path | None = None) -> Check:
    text = path.read_text(encoding="utf-8")
    lines = [ln.strip() for ln in text.splitlines()]
    body: list[str] = []
    in_subckt = 0
    for line in lines:
        low = line.lower()
        if low.startswith(".subckt"):
            in_subckt += 1
            continue
        if low.startswith(".ends"):
            in_subckt = max(0, in_subckt - 1)
            continue
        if in_subckt == 0 and line and not line.startswith(("*", ".", "+")):
            body.append(line)
    devices: dict[str, list[str]] = {}
    for line in body:
        parts = line.split()
        devices[parts[0]] = parts[1:]
    problems: list[str] = []
    if not lines or lines[-1] != ".end":
        problems.append("netlist does not end with .end")
    ground = spec["ground"]
    for comp in spec["components"]:
        ref = comp["ref"]
        info = resolve_model(spec, comp.get("model"), base_dir)
        if info and info.get("subckt"):
            order = comp.get("subckt_pins") or info.get("subckt_pins") or []
            element = "X" + ref
            if element not in devices:
                problems.append(f"{ref} ({element}) is missing from the netlist")
                continue
            definition = re.compile(rf"^\s*\.subckt\s+{re.escape(info['name'])}\b",
                                    re.IGNORECASE)
            if not any(definition.match(line) for line in lines):
                problems.append(
                    f"{ref}: model {info['name']!r} has no .subckt definition in the netlist"
                )
            expected = [gen.spice_net(comp["pins"][canonical_pin(comp["type"], pin)], ground)
                        for pin in order]
            actual = devices[element]
            if actual[:len(expected)] != expected:
                problems.append(f"{ref}: subckt nodes {actual[:len(expected)]} != {expected}")
            elif actual[-1] != info["name"]:
                problems.append(f"{ref}: subckt model {actual[-1]!r} != {info['name']!r}")
            continue

        element = gen.spice_ref(ref, comp["type"])
        if element not in devices:
            problems.append(f"{ref} ({element}) is missing from the netlist")
            continue
        expected = [gen.spice_net(comp["pins"][k], ground)
                    for k in sorted(comp["pins"]) if k in comp["pins"]]
        actual = devices[element]
        n = len(expected)
        if comp["type"] in ("NMOS", "PMOS"):
            # Bulk is tied to source in the emitted line.
            actual_nodes = actual[:4]
            if actual_nodes[:3] != expected[:3] or actual_nodes[3] != expected[2]:
                problems.append(f"{ref}: nodes {actual_nodes} != {expected[:3]}+source")
            continue
        if comp["type"] == "OPAMP":
            n = 5
        if actual[:n] != expected[:n]:
            problems.append(f"{ref}: nodes {actual[:n]} != {expected[:n]}")
    if problems:
        return Check("spice_netlist", False, "; ".join(problems), {"devices": len(devices)})
    return Check("spice_netlist", True,
                 f"{len(devices)} devices, node lists match the spec",
                 {"devices": len(devices)})


def _parse_asc(text: str) -> tuple[dict[str, dict[str, Any]], dict[tuple[int, int], str]]:
    symbols: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    flags: dict[tuple[int, int], str] = {}
    for raw in text.splitlines():
        parts = raw.split()
        if not parts:
            continue
        if parts[0] == "SYMBOL":
            current = {"symbol": parts[1], "x": int(parts[2]), "y": int(parts[3]),
                       "rot": parts[4] if len(parts) > 4 else "R0"}
        elif parts[0] == "SYMATTR" and current is not None and len(parts) >= 3:
            if parts[1] == "InstName":
                current["ref"] = parts[2]
                symbols[parts[2]] = current
            elif parts[1] == "Value":
                current["value"] = " ".join(parts[2:])
            elif parts[1] == "Prefix":
                current["prefix"] = parts[2]
        elif parts[0] == "FLAG":
            flags[(int(parts[1]), int(parts[2]))] = parts[3]
    return symbols, flags


def check_asc(spec: dict[str, Any], path: Path,
              base_dir: str | Path | None = None) -> Check:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("Version "):
        return Check("ltspice_asc", False, "missing Version header")
    symbols, flags = _parse_asc(text)
    problems: list[str] = []
    ground = spec["ground"]
    for comp in spec["components"]:
        ref = comp["ref"]
        if ref not in symbols:
            problems.append(f"{ref} is missing from the .asc")
            continue
        entry = symbols[ref]
        info = resolve_model(spec, comp.get("model"), base_dir)
        if info and info.get("subckt"):
            if entry.get("prefix") != "X":
                problems.append(f"{ref}: subcircuit instance needs Prefix X")
            if entry.get("value") != info["name"]:
                problems.append(
                    f"{ref}: Value {entry.get('value')!r} != subckt {info['name']!r}")
        rot_text = entry["rot"]
        try:
            rot = int(rot_text.lstrip("R"))
        except ValueError:
            problems.append(f"{ref}: bad rotation {rot_text!r}")
            continue
        if "M" in rot_text:
            problems.append(f"{ref}: mirrored symbols are not covered by the checker")
            continue
        pins = asc_pins(comp["type"], entry["x"], entry["y"], rot, False)
        for pin, (x, y) in pins.items():
            expected = gen.spice_net(comp["pins"][pin], ground)
            found = flags.get((x, y))
            if found is None:
                problems.append(f"{ref} pin {pin}: no FLAG at {(x, y)}")
            elif found != expected:
                problems.append(f"{ref} pin {pin}: FLAG {found!r} != {expected!r}")
    if problems:
        return Check("ltspice_asc", False, "; ".join(problems),
                     {"symbols": len(symbols), "flags": len(flags)})
    return Check("ltspice_asc", True,
                 f"{len(symbols)} symbols; every pin has a matching net flag",
                 {"symbols": len(symbols), "flags": len(flags)})


def _netlist_nets(root: Any) -> dict[str, set[tuple[str, str]]]:
    nets: dict[str, set[tuple[str, str]]] = {}
    for section in sexp_children(root, "nets"):
        for net in sexp_children(section, "net"):
            name = None
            nodes: set[tuple[str, str]] = set()
            for item in net:
                if isinstance(item, list) and item and item[0] == "name":
                    name = item[1]
                elif isinstance(item, list) and item and item[0] == "node":
                    ref = pin = None
                    for sub in item:
                        if isinstance(sub, list) and sub[0] == "ref":
                            ref = sub[1]
                        elif isinstance(sub, list) and sub[0] == "pin":
                            pin = sub[1]
                    if ref and pin:
                        nodes.add((ref, pin))
            if name is not None:
                nets[name] = nodes
    return nets


def check_kicad_net(spec: dict[str, Any], path: Path) -> Check:
    try:
        root = parse_sexp(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return Check("kicad_netlist", False, f"cannot parse: {exc}")
    nets = _netlist_nets(root)
    expected: dict[str, set[tuple[str, str]]] = {}
    for comp in spec["components"]:
        for pin, net in comp["pins"].items():
            expected.setdefault(net, set()).add((comp["ref"], pin))
    problems = []
    for net, nodes in expected.items():
        if net not in nets:
            problems.append(f"net {net!r} is missing")
        elif nets[net] != nodes:
            problems.append(f"net {net!r}: {sorted(nets[net])} != {sorted(nodes)}")
    for net in nets:
        if net not in expected:
            problems.append(f"unexpected net {net!r}")
    if problems:
        return Check("kicad_netlist", False, "; ".join(problems))
    return Check("kicad_netlist", True,
                 f"{len(nets)} nets match the spec exactly", {"nets": len(nets)})


def _load_kiutils():
    try:
        from kiutils.schematic import Schematic  # type: ignore
        return Schematic
    except ImportError:
        return None


def _kicad_pin_world(sym, pin, sx: float, sy: float, angle: int,
                     mirror: str | None) -> tuple[float, float]:
    px, py = float(pin.position.X), float(pin.position.Y)
    rot = angle % 360
    if rot == 0:
        rx, ry = px, py
    elif rot == 90:
        rx, ry = -py, px
    elif rot == 180:
        rx, ry = -px, -py
    elif rot == 270:
        rx, ry = py, -px
    else:
        raise ValueError(f"unsupported angle {angle}")
    if mirror == "x":
        ry = -ry
    elif mirror == "y":
        rx = -rx
    return sx + rx, sy - ry


def check_kicad_sch(spec: dict[str, Any], path: Path) -> Check:
    Schematic = _load_kiutils()
    if Schematic is None:
        return Check("kicad_schematic", None,
                     "kiutils is not installed; schematic geometry not parsed "
                     "(set PYTHONPATH to a kiutils checkout)")
    try:
        sch = Schematic.from_file(str(path))
    except Exception as exc:  # kiutils raises a variety of parser errors
        return Check("kicad_schematic", False, f"kiutils could not parse: {exc}")

    libs = {lib.libId: lib for lib in sch.libSymbols}
    labels: dict[tuple[float, float], set[str]] = {}
    for label in sch.globalLabels:
        key = (round(float(label.position.X), 3), round(float(label.position.Y), 3))
        labels.setdefault(key, set()).add(label.text)

    by_ref: dict[str, Any] = {}
    for sym in sch.schematicSymbols:
        ref = next((p.value for p in sym.properties if p.key == "Reference"), None)
        if ref:
            by_ref[ref] = sym

    problems: list[str] = []
    for comp in spec["components"]:
        ref = comp["ref"]
        sym = by_ref.get(ref)
        if sym is None:
            problems.append(f"{ref} is missing from the schematic")
            continue
        lib = libs.get(sym.libId)
        if lib is None:
            problems.append(f"{ref}: library symbol {sym.libId!r} is not embedded")
            continue
        pins = {p.number: p for unit in lib.units for p in unit.pins}
        angle = int(getattr(sym.position, "angle", 0) or 0)
        mirror = getattr(sym, "mirror", None)
        for pin_num, net in comp["pins"].items():
            pin = pins.get(pin_num)
            if pin is None:
                problems.append(f"{ref}: pin {pin_num} is not in the embedded symbol")
                continue
            x, y = _kicad_pin_world(sym, pin, float(sym.position.X),
                                    float(sym.position.Y), angle, mirror)
            found = labels.get((round(x, 3), round(y, 3)), set())
            if net not in found:
                problems.append(
                    f"{ref} pin {pin_num}: no global label {net!r} at "
                    f"({x:.2f}, {y:.2f}); found {sorted(found) or 'nothing'}"
                )
    if problems:
        return Check("kicad_schematic", False, "; ".join(problems[:12]),
                     {"symbols": len(by_ref), "labels": len(sch.globalLabels)})
    return Check("kicad_schematic", True,
                 f"{len(by_ref)} symbols; every pin endpoint carries the correct global label",
                 {"symbols": len(by_ref), "labels": len(sch.globalLabels)})


# ---------------------------------------------------------------------------
# Electrical checks
# ---------------------------------------------------------------------------

def _find_components(spec: dict[str, Any], ctype: str) -> list[dict[str, Any]]:
    return [c for c in spec["components"] if c["type"] == ctype]


def _source_node(spec: dict[str, Any]) -> str | None:
    for comp in spec["components"]:
        if comp["type"] == "V" and float(comp.get("ac", 0.0) or 0.0) > 0:
            return comp["pins"]["1"]
    return None


def _rc_values(spec: dict[str, Any]) -> tuple[float, float] | None:
    resistors = _find_components(spec, "R")
    capacitors = _find_components(spec, "C")
    if len(resistors) != 1 or len(capacitors) != 1:
        return None
    return float(resistors[0]["value"]), float(capacitors[0]["value"])


def _divider_expected(spec: dict[str, Any]) -> tuple[float, float] | None:
    """Return (vin, vout) for a two-resistor divider, else None."""
    resistors = _find_components(spec, "R")
    if len(resistors) != 2:
        return None
    sources = [c for c in _find_components(spec, "V") if float(c.get("dc", 0.0) or 0.0) != 0]
    if len(sources) != 1:
        return None
    ground = spec["ground"]
    r1, r2 = resistors
    if {r1["pins"]["2"], r2["pins"]["1"]} - {ground} == set():
        return None
    out = r1["pins"]["2"]
    if r2["pins"]["1"] != out or r2["pins"]["2"] != ground:
        return None
    if r1["pins"]["1"] != sources[0]["pins"]["1"]:
        return None
    vin = float(sources[0].get("dc", 0.0) or 0.0)
    vout = vin * float(r2["value"]) / (float(r1["value"]) + float(r2["value"]))
    return vin, vout


def check_mna(spec: dict[str, Any]) -> list[Check]:
    checks: list[Check] = []
    nonlinear = [c["ref"] for c in spec["components"] if c["type"] not in LINEAR_TYPES]
    if nonlinear:
        return [Check("mna_linear", None,
                      "circuit contains nonlinear element(s) "
                      f"{', '.join(nonlinear)}; MNA only solves linear circuits")]
    try:
        circuit = from_components(
            spec["components"], ground=spec["ground"],
            supplies={s["name"]: float(s["voltage"]) for s in spec.get("supplies", [])},
        )
    except (MNAError, UnsupportedElement, ValueError) as exc:
        return [Check("mna_linear", False, f"cannot build MNA circuit: {exc}")]

    try:
        dc = circuit.solve_dc()
    except MNAError as exc:
        return [Check("mna_dc", False, f"DC solve failed: {exc}")]

    node_voltages = {k: v.real for k, v in dc.items() if not k.startswith("@")}
    checks.append(Check("mna_dc", True,
                        "DC operating point solved; node voltages: " +
                        ", ".join(f"{k}={v:.4g} V" for k, v in sorted(node_voltages.items())),
                        {"node_voltages_V": {k: round(v, 9) for k, v in node_voltages.items()}}))

    # Divider ratio
    divider = _divider_expected(spec)
    if divider is not None and spec.get("topology") in ("divider", "custom"):
        _vin, expected = divider
        out = _find_components(spec, "R")[0]["pins"]["2"]
        actual = dc[out].real
        ok = abs(actual - expected) <= 1e-6 * max(1.0, abs(expected))
        checks.append(Check(
            "divider_ratio", ok,
            f"V({out}) = {actual:.9g} V, expected {expected:.9g} V "
            f"(error {abs(actual - expected) / max(abs(expected), 1e-12) * 100:.3g}%)",
            {"actual_V": actual, "expected_V": expected},
        ))

    # RC corner
    topology = str(spec.get("topology", ""))
    rc = _rc_values(spec)
    if rc is not None and topology in ("rc_lowpass", "rc_highpass"):
        r, c = rc
        expected = 1.0 / (2 * math.pi * r * c)
        out = _find_components(spec, "C")[0]["pins"]["1"]
        if topology == "rc_lowpass":
            out = _find_components(spec, "C")[0]["pins"]["1"]
            passband = "low"
        else:
            out = _find_components(spec, "R")[0]["pins"]["1"]
            passband = "high"
        try:
            actual = circuit.find_cutoff(out, spec["components"][0]["pins"]["1"],
                                         passband=passband, f_min=1.0, f_max=1e8)
        except MNAError as exc:
            checks.append(Check("rc_corner", False, f"cutoff search failed: {exc}"))
        else:
            ok = abs(actual - expected) / expected < 0.01
            checks.append(Check(
                "rc_corner", ok,
                f"-3 dB at {si_number(actual, 'Hz')}, closed form "
                f"1/(2*pi*R*C) = {si_number(expected, 'Hz')}",
                {"actual_Hz": actual, "expected_Hz": expected},
            ))

    # Sallen-Key corner against the design target
    if "sallen_key" in topology and spec.get("parameters", {}).get("fc"):
        target = float(spec["parameters"]["fc"])
        out = "OUT" if any("OUT" in c["pins"].values() for c in spec["components"]) else None
        if out is None:
            out = next((c["pins"]["5"] for c in spec["components"] if c["type"] == "OPAMP"), None)
        inp = _source_node(spec)
        if out and inp:
            passband = "low" if "lp" in topology or "lowpass" in topology else "high"
            # Keep the reference point inside the filter passband but well
            # below the op-amp's finite GBW, otherwise a "high" reference at
            # 1e8 Hz measures the op-amp roll-off, not the filter corner.
            try:
                actual = circuit.find_cutoff(out, inp, passband=passband,
                                             f_min=max(0.1, target / 1000.0),
                                             f_max=target * 10.0)
            except MNAError as exc:
                checks.append(Check("sallen_key_corner", False, f"cutoff search failed: {exc}"))
            else:
                ok = abs(actual - target) / target < 0.03
                checks.append(Check(
                    "sallen_key_corner", ok,
                    f"-3 dB at {si_number(actual, 'Hz')}, design target "
                    f"{si_number(target, 'Hz')} ({(actual - target) / target * 100:+.2f}%)",
                    {"actual_Hz": actual, "target_Hz": target},
                ))

    # Closed-loop op-amp gain checks for supported idealized topologies.
    if topology in ("opamp_noninverting", "noninverting", "opamp_inverting", "inverting"):
        rf = next((c for c in spec["components"] if c["ref"].upper().startswith("RF")), None)
        feedback = next((c for c in spec["components"] if c["ref"].upper().startswith(("RG", "RIN"))), None)
        out = next((c["pins"]["5"] for c in spec["components"] if c["type"] == "OPAMP"), None)
        inp = _source_node(spec)
        if rf and feedback and out and inp:
            ratio = float(rf["value"]) / float(feedback["value"])
            expected = 1 + ratio if topology in ("opamp_noninverting", "noninverting") else ratio
            actual = abs(circuit.ac_gain(out, inp, 10.0))
            ok = abs(actual - expected) / expected < 0.01
            formula = "1+Rf/Rg" if topology in ("opamp_noninverting", "noninverting") else "Rf/Rin"
            checks.append(Check(
                "opamp_gain", ok,
                f"|gain(10 Hz)| = {actual:.6g}, expected |gain|={formula}={expected:.6g}",
                {"actual": actual, "expected": expected, "topology": topology},
            ))

    # Generic AC sanity: no singular solve, finite response.
    if _source_node(spec):
        try:
            sweep = circuit.solve_ac([10.0, 1e3, 1e5])
            finite = all(math.isfinite(abs(v)) for sol in sweep.values() for v in sol.values())
            checks.append(Check("mna_ac", finite,
                                "AC solve at 10 Hz / 1 kHz / 100 kHz is finite"
                                if finite else "AC solve produced non-finite values"))
        except MNAError as exc:
            checks.append(Check("mna_ac", False, f"AC solve failed: {exc}"))
    return checks


def _load_sibling_spec_module(skill_dir: Path):
    import importlib.util

    path = skill_dir / "scripts" / "design_spec.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("pcb_sibling_design_spec", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def check_pcb_handoff(spec: dict[str, Any], out: Path) -> Check:
    """Check that the PCB overlay, pin map and aliases agree with the netlist."""
    from pcb_handoff import mapping_pad_nets  # noqa: PLC0415

    name = spec["name"]
    paths = {
        "overlay": out / f"{name}-pcb.json",
        "pin_map": out / f"{name}-pinmap.json",
        "aliases": out / f"{name}-node-aliases.json",
        "handoff": out / f"{name}-pcb-handoff.md",
    }
    if not any(path.exists() for path in paths.values()):
        return Check("pcb_handoff", None,
                     'not generated (add "pcb" to deliverables)')
    missing_files = [key for key, path in paths.items() if not path.exists()]
    if missing_files:
        return Check("pcb_handoff", False,
                     "missing handoff file(s): " + ", ".join(missing_files))
    try:
        overlay = json.loads(paths["overlay"].read_text(encoding="utf-8"))
        pin_map = json.loads(paths["pin_map"].read_text(encoding="utf-8"))
        aliases = json.loads(paths["aliases"].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return Check("pcb_handoff", False, f"cannot read handoff JSON: {exc}")

    problems: list[str] = []
    if overlay.get("version") != 1:
        problems.append("overlay version is not 1")
    if not overlay.get("profile"):
        problems.append("overlay has no profile")
    board = overlay.get("board") or {}
    for key in ("width_mm", "height_mm", "layers"):
        if key not in board:
            problems.append(f"overlay board is missing {key}")
    if not overlay.get("netclasses", {}).get("Default"):
        problems.append("overlay has no Default netclass")
    for net in overlay.get("nets", []):
        cls = net.get("class", "Default")
        if cls not in overlay.get("netclasses", {}):
            problems.append(f"net {net.get('name')!r} uses undefined class {cls!r}")

    included = set(pin_map.get("include") or [])
    excluded = set(pin_map.get("exclude") or [])
    expected: set[str] = set()
    for comp in spec["components"]:
        ctype, ref = comp["type"], comp["ref"]
        element = spice_ref(ref, ctype)
        if ctype in SIMULATION_ONLY_TYPES:
            continue
        if ctype == "SW" and element not in included:
            continue
        if ctype in BOARDABLE_TYPES or element in included:
            if element not in excluded:
                expected.add(element)
    overlay_refs = {str(comp.get("ref")) for comp in overlay.get("components", [])}
    missing = sorted(expected - overlay_refs)
    if missing:
        problems.append("boardable parts missing from overlay: " + ", ".join(missing))

    refs = pin_map.get("refs") or {}
    by_element = {spice_ref(c["ref"], c["type"]): c for c in spec["components"]}
    for ref, mapping in refs.items():
        if ref not in overlay_refs:
            problems.append(f"pin map references {ref!r}, which is not in the overlay")
        comp = by_element.get(ref)
        if comp is None:
            continue
        pad_nets: dict[str, str] = {}
        for pad, net in mapping_pad_nets(comp, mapping):
            previous = pad_nets.get(pad)
            if previous is not None and previous != net:
                problems.append(
                    f"pin map {ref!r} maps pad {pad!r} to both "
                    f"{previous!r} and {net!r}"
                )
            pad_nets[pad] = net

    if str(aliases.get("0")) != str(spec["ground"]):
        problems.append(
            f'node alias "0" is {aliases.get("0")!r}, expected {spec["ground"]!r}'
        )

    missing_footprints = [
        comp["ref"] for comp in overlay.get("components", [])
        if not str(comp.get("footprint") or "").strip()
    ]
    defaults = pin_map.get("defaults") or {}
    missing_pin_maps: list[str] = []
    for comp in overlay.get("components", []):
        props = comp.get("properties") or {}
        raw = props.get("logical_pins")
        if not raw:
            continue
        try:
            logical = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if len(logical) <= 2:
            continue
        ref = str(comp.get("ref"))
        prefix = ref[:1].upper()
        if ref in refs or prefix in defaults:
            continue
        missing_pin_maps.append(str(props.get("circuit_ref") or ref))
    detail = (
        f"{len(overlay_refs)} board parts, {len(refs)} pin map(s), "
        f"{len(aliases)} node alias(es); "
        f"{len(missing_footprints)} footprint(s) still to choose; "
        f"{len(missing_pin_maps)} pin map(s) still to choose"
    )
    if problems:
        return Check("pcb_handoff", False, "; ".join(problems),
                     {"missing_footprints": missing_footprints,
                      "missing_pin_maps": missing_pin_maps})

    # Optional: run the sibling skill's own validator when all footprints are
    # present.  This turns the handoff from "structurally consistent" into
    # "valid against the PCB skill's rules".  It is skipped, not passed, when
    # the sibling is not installed.
    from pcb_handoff import find_pcb_skill  # noqa: PLC0415

    skill_dir = find_pcb_skill()
    if skill_dir is None:
        return Check("pcb_handoff", True,
                     detail + "; sibling pcb-design-skill not found, "
                     "PCB-spec validation skipped")
    if missing_footprints:
        return Check("pcb_handoff", True,
                     detail + "; PCB-spec validation deferred until footprints are chosen")
    try:
        sibling = _load_sibling_spec_module(skill_dir)
        if sibling is None:
            return Check("pcb_handoff", True,
                         detail + "; sibling design_spec.py not found")
        profile = sibling.load_profile(str(overlay["profile"]))
        findings = sibling.validate_spec(sibling.apply_defaults(overlay), profile)
    except Exception as exc:  # noqa: BLE001 - report, do not hide a validator crash
        return Check("pcb_handoff", False,
                     f"sibling PCB validator failed: {exc}")
    if findings.errors:
        errors = "; ".join(item.get("message", str(item)) for item in findings.errors)
        return Check("pcb_handoff", False,
                     f"sibling PCB validation failed: {errors}")
    return Check(
        "pcb_handoff", True,
        detail + f"; sibling PCB validation passed with "
        f"{len(findings.warnings)} warning(s)",
        {"warnings": findings.warnings,
         "missing_footprints": missing_footprints,
         "missing_pin_maps": missing_pin_maps},
    )


def _simulate_ngspice(netlist: Path) -> tuple[Check, dict[str, ngspice_raw.Plot], str]:
    """Run ngspice and return its check, parsed plots and combined log text."""
    capability = env_probe.probe_ngspice()
    if not capability.get("supported"):
        if capability.get("ok"):
            return Check("ngspice", None,
                         "ngspice is installed but incompatible with this machine ("
                         + capability.get("detail", "no reason reported")
                         + "); nonlinear devices were not simulated"), {}, ""
        return Check("ngspice", None,
                     "ngspice is not installed; nonlinear devices were not simulated"), {}, ""
    exe = capability.get("path")
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "ngspice.log"
        raw = Path(td) / "ngspice.raw"
        try:
            proc = subprocess.run(
                # -r writes every analysis to a rawfile. Without it, ngspice 42
                # exits 1 when an .ac analysis produces no printable output.
                # ASCII keeps the rawfile readable without a parser dependency.
                [exe, "-b", "-o", str(log), "-r", str(raw), str(netlist)],
                capture_output=True, text=True, timeout=120,
                env={**os.environ, "SPICE_ASCIIRAWFILE": "1"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Check("ngspice", False, f"ngspice could not run: {exc}"), {}, ""
        log_text = log.read_text(errors="replace") if log.exists() else ""
        # ngspice ignores .four when a rawfile is requested.  Re-run without
        # -r so a declared thd_percent metric can read the Fourier block.
        fourier_text = ""
        if ".fourier line ignored" in log_text.lower():
            flog = Path(td) / "ngspice-fourier.log"
            try:
                subprocess.run(
                    [exe, "-b", "-o", str(flog), str(netlist)],
                    capture_output=True, text=True, timeout=120,
                    env={**os.environ},
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
            if flog.exists():
                fourier_text = flog.read_text(errors="replace")
        text = log_text + fourier_text + proc.stdout + proc.stderr
        evidence = {
            "returncode": proc.returncode,
            "stderr": (proc.stderr or "")[-1500:],
            "stdout": (proc.stdout or "")[-1500:],
            "log": log_text[-3000:],
        }
        if "singular matrix" in text.lower() or "doanalyses: iteration limit reached" in text.lower():
            return Check("ngspice", False,
                         "ngspice reported a singular matrix or non-convergence",
                         evidence), {}, text
        if proc.returncode != 0:
            return Check("ngspice", False, f"ngspice exited with {proc.returncode}",
                         evidence), {}, text

        # Parse the ASCII rawfile. A completed run with plots is evidence even
        # when the operating-point table is not printable.
        raw_text = raw.read_text(errors="replace") if raw.exists() else ""
        plots = ngspice_raw.parse_raw(raw_text)
        voltages: dict[str, float] = {}
        op_plot = ngspice_raw.find_plot(plots, "op")
        if op_plot is not None and op_plot.points:
            for var, value in zip(op_plot.variables, op_plot.points[0]):
                if not var.lower().startswith("v("):
                    continue
                try:
                    voltages[var[2:-1]] = float(value)
                except (TypeError, ValueError):
                    pass
        if plots:
            detail = (f"ngspice ran {len(plots)} analysis plot(s): "
                      + ", ".join(plots))
            if voltages:
                detail += "; " + ", ".join(
                    f"V({k})={v:.6g}" for k, v in voltages.items())
            return Check("ngspice", True, detail,
                         {**evidence, "plots": list(plots),
                          "node_voltages_V": voltages}), plots, text

        # Fall back to the printed table when a rawfile was not produced.
        in_table = False
        for line in text.splitlines():
            if re.match(r"\s*Node\s+Voltage", line):
                in_table = True
                continue
            if in_table:
                if not line.strip() or line.strip().startswith("-"):
                    if voltages:
                        break
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        voltages[parts[0]] = float(parts[1])
                    except ValueError:
                        in_table = False
        if voltages:
            return Check("ngspice", True,
                         "ngspice ran and reported " +
                         ", ".join(f"V({k})={v:.6g}" for k, v in voltages.items()),
                         {"node_voltages_V": voltages}), {}, text
        return Check("ngspice", None,
                     "ngspice completed but its output format could not be parsed; "
                     "see the log for the raw result", {"log": text[-2000:]}), {}, text


def _run_ngspice(netlist: Path) -> Check:
    """Backward-compatible wrapper for callers that only need the check."""
    check, _, _ = _simulate_ngspice(netlist)
    return check


def check_metrics(spec: dict[str, Any], plots: dict[str, ngspice_raw.Plot],
                  log_text: str, ngspice_check: Check) -> Check | None:
    """Evaluate declared metrics, or return None when none are declared.

    A metric that cannot be evaluated is a failure once ngspice has run.  When
    ngspice is unavailable the metric check is a skip, matching the rest of the
    pipeline's "never call a skip a pass" rule.
    """
    block = spec.get("metrics") or {}
    items = block.get("metrics") if isinstance(block, dict) else None
    if not items:
        return None
    if ngspice_check.ok is None:
        return Check("metrics", None,
                     "ngspice unavailable; declared metrics were not evaluated")
    if ngspice_check.ok is False:
        return Check("metrics", False,
                     "ngspice failed; declared metrics were not evaluated")
    results, errors = metric_lib.evaluate(block, plots, log_text)
    if errors:
        return Check("metrics", False, "; ".join(errors),
                     {"metrics": [result.as_dict() for result in results]})
    if not results:
        return Check("metrics", False, "no metrics could be evaluated")
    verdicts: list[str] = []
    failed = False
    enriched: list[dict[str, Any]] = []
    for result in results:
        ok, rule = metric_lib.compare(result.metric, result.value)
        record = result.as_dict()
        record.update({"ok": ok, "rule": rule})
        enriched.append(record)
        mark = "PASS" if ok is True else "FAIL" if ok is False else "INFO"
        verdicts.append(
            f"{record['id'] or record['measure']}: {record['measure']}="
            f"{record['value']:.6g} {record['unit']} ({mark}; {rule})")
        if ok is False:
            failed = True
    return Check("metrics", not failed, "; ".join(verdicts),
                 {"metrics": enriched, "conditions": block.get("conditions", {})})


def check_models(spec: dict[str, Any],
                 base_dir: str | Path | None = None) -> Check:
    """Report whether semiconductors are backed by real, traceable models."""
    status = model_status(spec, base_dir)
    records = model_records(spec, base_dir)
    if not records:
        return Check("models", None, status)
    generic = [r["ref"] for r in records if r["generic"]]
    detail = status
    if generic:
        detail += f"; generic placeholder for {', '.join(generic)}"
    elif status.startswith("declared"):
        detail += ("; models are declared inline but have no vendor/part/source; "
                   "record provenance to call them vendor models")
    evidence = {"status": status, "devices": [
        {"ref": r["ref"], "model": r["model"],
         "kind": (r["resolved"] or {}).get("kind", "generic")}
        for r in records
    ]}
    if status.startswith("model errors"):
        return Check("models", False, detail, evidence)
    if status == "vendor models supplied":
        return Check("models", True, detail, evidence)
    return Check("models", None, detail, evidence)


def check_design(spec: dict[str, Any]) -> Check:
    """Report whether the planning and evidence record is complete."""
    design = spec.get("design")
    if not design:
        return Check(
            "design", None,
            "no design record; add requirements, architecture, decisions, "
            "risks and sources (see references/design-process.md)",
        )
    if not isinstance(design, dict):
        return Check("design", False, "'design' must be an object")

    requirements = design.get("requirements") or []
    decisions = design.get("decisions") or []
    risks = design.get("risks") or []
    sources = design.get("sources") or []
    architecture = design.get("architecture") or {}
    evidence = {
        "requirements": len(requirements),
        "decisions": len(decisions),
        "risks": len(risks),
        "sources": len(sources),
    }

    missing = []
    if not requirements:
        missing.append("requirements")
    if not decisions:
        missing.append("decisions")
    if not risks:
        missing.append("risks")
    if not sources:
        missing.append("sources")
    if not (architecture.get("summary") or architecture.get("blocks")):
        missing.append("architecture summary or blocks")
    if missing:
        return Check("design", None,
                     "design record incomplete: missing " + ", ".join(missing),
                     evidence)

    unsourced = [str(d.get("id") or "?") for d in decisions
                 if not (d.get("sources") or d.get("rationale"))]
    if unsourced:
        return Check("design", None,
                     "decisions without a source or rationale: " + ", ".join(unsourced),
                     evidence)

    high = [str(r.get("id") or "?") for r in risks
            if r.get("severity") == "high"
            and not (r.get("mitigation") and r.get("verification"))]
    if high:
        return Check("design", None,
                     "high risks need mitigation and verification: " + ", ".join(high),
                     evidence)

    return Check(
        "design", True,
        f"{len(requirements)} requirements, {len(decisions)} decisions, "
        f"{len(risks)} risks, {len(sources)} sources",
        evidence,
    )


def check_operating_limits(spec: dict[str, Any]) -> Check:
    """Check explicitly calculated stresses against sourced datasheet limits.

    This check validates the supplied numeric stress and limit record; it does
    not independently derive stresses from a SPICE operating point.
    """
    design = spec.get("design") or {}
    limits = design.get("operating_limits") or []
    if not limits:
        return Check(
            "operating_limits", None,
            "no operating limits declared; add calculated stresses and sourced datasheet limits",
        )
    if not isinstance(limits, list):
        return Check("operating_limits", False, "design.operating_limits must be a list")

    evidence: list[dict[str, Any]] = []
    failures: list[str] = []
    for index, item in enumerate(limits):
        if not isinstance(item, dict):
            failures.append(f"item {index + 1} is not an object")
            continue
        ident = str(item.get("id") or f"item-{index + 1}")
        required_fields = ("parameter", "value", "unit", "condition", "source")
        missing = [field for field in required_fields if item.get(field) in (None, "")]
        if missing:
            failures.append(f"{ident}: missing " + ", ".join(missing))
            continue
        try:
            value = float(item["value"])
            lower = float(item["min"]) if item.get("min") is not None else None
            upper = float(item["max"]) if item.get("max") is not None else None
        except (TypeError, ValueError):
            failures.append(f"{ident}: value/min/max must be numeric")
            continue
        if not math.isfinite(value) or (lower is not None and not math.isfinite(lower)) or (upper is not None and not math.isfinite(upper)):
            failures.append(f"{ident}: value/min/max must be finite")
            continue
        if lower is None and upper is None:
            failures.append(f"{ident}: at least one of min or max is required")
            continue
        if lower is not None and upper is not None and lower > upper:
            failures.append(f"{ident}: min exceeds max")
            continue
        passed = (lower is None or value >= lower) and (upper is None or value <= upper)
        record = {
            "id": ident, "parameter": item["parameter"], "value": value,
            "unit": item["unit"], "min": lower, "max": upper,
            "condition": item["condition"], "source": item["source"],
            "margin_to_min": value - lower if lower is not None else None,
            "margin_to_max": upper - value if upper is not None else None,
            "passed": passed,
        }
        evidence.append(record)
        if not passed:
            failures.append(
                f"{ident}: {item['parameter']}={value:g} {item['unit']} "
                f"outside [{lower if lower is not None else '-inf'}, "
                f"{upper if upper is not None else '+inf'}]"
            )

    if failures:
        return Check("operating_limits", False, "; ".join(failures), evidence)
    return Check(
        "operating_limits", True,
        f"{len(evidence)} calculated stresses are within their declared sourced limits",
        evidence,
    )


def check_measurement_plan(spec: dict[str, Any]) -> Check:
    """Check that every declared measurement is actionable and traces to a requirement."""
    design = spec.get("design") or {}
    measurements = design.get("measurements") or []
    if not measurements:
        return Check("measurement_plan", None,
                     "no measurement plan declared; add design.measurements with test points, setup and acceptance criteria")
    if not isinstance(measurements, list):
        return Check("measurement_plan", False, "design.measurements must be a list")
    requirement_ids = {
        str(item.get("id")) for item in (design.get("requirements") or [])
        if isinstance(item, dict) and item.get("id")
    }
    seen: set[str] = set()
    problems: list[str] = []
    for index, item in enumerate(measurements):
        if not isinstance(item, dict):
            problems.append(f"measurement {index + 1} is not an object")
            continue
        ident = str(item.get("id") or f"measurement-{index + 1}")
        if ident in seen:
            problems.append(f"{ident}: duplicate measurement id")
        seen.add(ident)
        required_fields = ("requirement", "test_point", "quantity", "method",
                           "equipment", "conditions", "acceptance")
        missing = [field for field in required_fields if item.get(field) in (None, "")]
        if missing:
            problems.append(f"{ident}: missing " + ", ".join(missing))
        requirement = str(item.get("requirement") or "")
        if requirement and requirement not in requirement_ids:
            problems.append(f"{ident}: unknown requirement {requirement!r}")
    if problems:
        return Check("measurement_plan", False, "; ".join(problems),
                     {"measurement_count": len(measurements), "requirement_ids": sorted(requirement_ids)})
    return Check("measurement_plan", True,
                 f"{len(measurements)} actionable measurements trace to declared requirements",
                 {"measurement_count": len(measurements), "requirement_ids": sorted(requirement_ids)})


def check_kicad_erc(sch: Path, out: Path) -> Check:
    """Run KiCad's own ERC, but only when kicad-cli is actually usable here."""
    capability = env_probe.probe_kicad_cli()
    if not capability.get("supported"):
        if capability.get("ok"):
            return Check("kicad_erc", None,
                         "kicad-cli is installed but incompatible with this machine ("
                         + capability.get("detail", "no reason reported") + "); ERC was not run")
        return Check("kicad_erc", None,
                     "kicad-cli is not installed; ERC was not run")
    exe = capability.get("path")
    report = out / f"{sch.stem}-erc.rpt"
    try:
        proc = subprocess.run(
            [exe, "sch", "erc", "--severity-error", "--exit-code-violations",
             "-o", str(report), str(sch)],
            capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("kicad_erc", False, f"kicad-cli ERC could not run: {exc}")
    text = report.read_text(errors="replace") if report.exists() else proc.stdout + proc.stderr
    violations = len(re.findall(r"^\s*\[error\]", text, flags=re.IGNORECASE | re.MULTILINE))
    evidence = {"report": str(report), "exit_code": proc.returncode,
                "report_text": text[-4000:]}
    if proc.returncode == 5:
        return Check("kicad_erc", False,
                     f"KiCad ERC found {violations or 'one or more'} error-level violation(s); "
                     f"see {report.name}", evidence)
    if proc.returncode != 0:
        return Check("kicad_erc", False,
                     f"kicad-cli ERC exited with {proc.returncode}", evidence)
    return Check("kicad_erc", True,
                 f"KiCad ERC reported no error-level violations ({report.name})", evidence)



def check_kicad_pcb_drc(board: Path, out: Path) -> Check:
    """Run KiCad PCB DRC against an actual board file; never treat a handoff as a board."""
    if not board.exists():
        return Check("kicad_pcb_drc", None,
                     f"actual PCB file not found ({board.name}); PCB handoff is not a board")
    capability = env_probe.probe_kicad_pcb_drc()
    if not capability.get("supported"):
        detail = capability.get("detail", "kicad-cli PCB DRC is unavailable or incompatible")
        return Check("kicad_pcb_drc", None, f"PCB DRC was not run: {detail}")
    exe = capability.get("path")
    report = out / f"{board.stem}-drc.rpt"
    try:
        proc = subprocess.run(
            [exe, "pcb", "drc", "--exit-code-violations", "-o", str(report), str(board)],
            capture_output=True, text=True, timeout=240,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("kicad_pcb_drc", False, f"kicad-cli PCB DRC could not run: {exc}")
    report_text = report.read_text(errors="replace") if report.exists() else ""
    combined = report_text + proc.stdout + proc.stderr
    evidence = {"board": str(board), "report": str(report),
                "exit_code": proc.returncode, "report_text": combined[-6000:]}
    if proc.returncode == 5:
        return Check("kicad_pcb_drc", False,
                     "KiCad PCB DRC reported violations; see " + report.name, evidence)
    if proc.returncode != 0:
        return Check("kicad_pcb_drc", False,
                     f"kicad-cli PCB DRC exited with {proc.returncode}", evidence)
    return Check("kicad_pcb_drc", True,
                 f"KiCad PCB DRC completed without error-level violations ({report.name})",
                 evidence)


def design_consistency(spec: dict[str, Any]) -> list[Check]:
    checks: list[Check] = []
    params = spec.get("parameters") or {}
    topology = str(spec.get("topology", ""))

    if topology in ("buck", "buck_converter") and params:
        try:
            from circuit_calc import calc_buck
            calc = calc_buck(params)
        except (ValueError, KeyError) as exc:
            return [Check("buck_sizing", False, f"buck calculation failed: {exc}")]
        if not calc.get("ok"):
            return [Check("buck_sizing", False, calc.get("error", "buck calculation failed"))]
        results = calc["results"]
        comps = spec["components"]
        ground = spec["ground"]
        inductors = [c for c in comps if c["type"] == "L"]
        capacitors = [c for c in comps if c["type"] == "C"]
        l = float(inductors[0]["value"]) if len(inductors) == 1 else None
        # Pick the output capacitor: the one from the inductor's output-side
        # node to ground.  This avoids accidentally checking an input capacitor.
        cout_comp = None
        if len(inductors) == 1:
            for net in inductors[0]["pins"].values():
                if net == ground:
                    continue
                candidates = [
                    c for c in capacitors
                    if net in c["pins"].values() and ground in c["pins"].values()
                ]
                if candidates:
                    cout_comp = candidates[0]
                    break
        if cout_comp is None and len(capacitors) == 1:
            cout_comp = capacitors[0]
        cout = float(cout_comp["value"]) if cout_comp else None
        problems = []
        if l is None or l < results["inductor_required_H"] * 0.95:
            problems.append(
                f"L={si_number(l or 0, 'H')} < required "
                f"{si_number(results['inductor_required_H'], 'H')}"
            )
        if cout is None or cout < results["output_cap_required_F"] * 0.95:
            problems.append(
                f"Cout={si_number(cout or 0, 'F')} < required "
                f"{si_number(results['output_cap_required_F'], 'F')}"
            )
        checks.append(Check("buck_sizing", not problems,
                            "; ".join(problems) if problems else
                            f"L={si_number(l, 'H')} and Cout={si_number(cout, 'F')} meet "
                            f"the CCM/ripple sizing equations",
                            {"duty": results["duty_cycle"]}))

    if topology == "led" and params:
        try:
            from circuit_calc import calc_led
            calc = calc_led(params)
            expected = calc["results"]["series_resistance_ohm"]
        except (ValueError, KeyError) as exc:
            return checks + [Check("led_sizing", False, f"LED calculation failed: {exc}")]
        r = next((float(c["value"]) for c in spec["components"]
                  if c["type"] == "R"), None)
        ok = r is not None and abs(r - expected) <= 1e-6 * max(expected, 1)
        checks.append(Check("led_sizing", ok,
                            f"series R={si_number(r or 0, 'ohm')}, design equation gives "
                            f"{si_number(expected, 'ohm')}",
                            {"actual_ohm": r, "expected_ohm": expected}))
    return checks


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def verify(spec_path: str | Path, outdir: str | Path,
           *, update_report: bool = True, pcb_board: str | None = None,
           pcb_profile: str | None = None, pcb_layers: int | None = None,
           pcb_skill: str | Path | None = None,
           erc: bool = False,
           required: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
    raw = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    base_dir = Path(spec_path).resolve().parent
    validation = validate_spec(raw, base_dir=base_dir)
    if not validation["ok"]:
        return {
            "ok": False,
            "checks": [Check("spec", False,
                             "invalid spec: " + "; ".join(validation["errors"])).as_dict()],
            "summary": "spec validation failed",
        }
    spec = validation["normalized"]
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    name = spec["name"]

    formats = [f for f in spec["deliverables"] if f in gen.ALL_FORMATS]
    gen.generate(
        spec_path, out, formats, pcb_board=pcb_board, pcb_profile=pcb_profile,
        pcb_layers=pcb_layers, pcb_skill=pcb_skill,
    )

    checks: list[Check] = [
        Check("spec", True,
              f"valid spec with {len(spec['components'])} components, "
              f"{len(validation['warnings'])} warning(s)",
              {"stats": validation.get("stats", {})}),
        check_models(spec, base_dir),
        check_design(spec),
        check_measurement_plan(spec),
        check_operating_limits(spec),
    ]

    paths = {
        "spice": out / f"{name}.cir",
        "ltspice_asc": out / f"{name}.asc",
        "kicad_net": out / f"{name}.net",
        "kicad_sch": out / f"{name}.kicad_sch",
    }
    if paths["spice"].exists():
        checks.append(check_spice(spec, paths["spice"], base_dir))
    if paths["ltspice_asc"].exists():
        checks.append(check_asc(spec, paths["ltspice_asc"], base_dir))
    if paths["kicad_net"].exists():
        checks.append(check_kicad_net(spec, paths["kicad_net"]))
    if paths["kicad_sch"].exists():
        checks.append(check_kicad_sch(spec, paths["kicad_sch"]))
    checks.append(check_pcb_handoff(spec, out))

    checks.extend(design_consistency(spec))
    checks.extend(check_mna(spec))

    if paths["spice"].exists():
        ngspice_check, plots, simulation_text = _simulate_ngspice(paths["spice"])
        checks.append(ngspice_check)
        metrics_check = check_metrics(spec, plots, simulation_text, ngspice_check)
        if metrics_check is not None:
            checks.append(metrics_check)
    elif isinstance(spec.get("metrics"), dict) and spec["metrics"].get("metrics"):
        checks.append(Check(
            "metrics", False,
            'metrics are declared but no SPICE netlist was generated; add "spice" '
            "to deliverables"))
    if erc and paths["kicad_sch"].exists():
        checks.append(check_kicad_erc(paths["kicad_sch"], out))
    checks.append(check_kicad_pcb_drc(out / f"{name}.kicad_pcb", out))

    if required:
        for stage in required:
            check_name = REQUIRED_CHECK.get(stage)
            check = next((c for c in checks if c.name == check_name), None)
            if check is None:
                checks.append(Check(
                    f"required:{stage}", False,
                    f"stage {stage!r} was required but its check did not run "
                    f"(expected {check_name!r})"))
            elif check.ok is not True:
                checks.append(Check(
                    f"required:{stage}", False,
                    f"stage {stage!r} was required but {check_name!r} did not pass: "
                    f"{check.detail}"))
            else:
                checks.append(Check(
                    f"required:{stage}", True,
                    f"stage {stage!r} required and passed ({check_name})"))

    failed = [c for c in checks if c.ok is False]
    skipped = [c for c in checks if c.ok is None]
    passed = [c for c in checks if c.ok is True]
    ok = not failed
    summary = (
        f"{len(passed)} passed, {len(skipped)} skipped, {len(failed)} failed"
    )
    result = {
        "ok": ok,
        "name": name,
        "generated": gen.date.today().isoformat(),
        "summary": summary,
        "checks": [c.as_dict() for c in checks],
        "warnings": validation["warnings"],
        "simulation_status": model_status(spec, base_dir),
        "provenance": provenance(required),
    }
    vpath = out / f"{name}-verification.json"
    vpath.write_text(dumps(result) + "\n", encoding="utf-8")

    if update_report:
        gen.generate(
            spec_path, out, formats, pcb_board=pcb_board,
            pcb_profile=pcb_profile, pcb_layers=pcb_layers,
            pcb_skill=pcb_skill,
        )
    return result


def _print(result: dict[str, Any]) -> None:
    status = "PASS" if result["ok"] else "FAIL"
    print(f"{status}: {result.get('name', '')} ({result.get('summary', '')})")
    for check in result["checks"]:
        mark = {True: "PASS", False: "FAIL", None: "SKIP"}[check["ok"]]
        print(f"  [{mark}] {check['name']}: {check['detail']}")
    prov = result.get("provenance") or {}
    if prov:
        where = prov.get("backend", "local")
        if prov.get("commit"):
            where += f" commit {str(prov['commit'])[:12]}"
        if prov.get("run_id"):
            where += f" run {prov['run_id']}"
        print(f"  backend: {where}")
    if result.get("warnings"):
        print("warnings:")
        for warning in result["warnings"]:
            print(f"  - {warning}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify.py",
        description="Verify a circuit design and record the evidence.",
    )
    parser.add_argument("spec")
    parser.add_argument("--out", required=True, help="directory with generated files")
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    parser.add_argument("--no-update-report", action="store_true",
                        help="do not regenerate the Markdown report with the results")
    parser.add_argument("--pcb-board", metavar="WxH[xL]",
                        help="PCB board size in mm for regeneration")
    parser.add_argument("--pcb-layers", type=int,
                        help="PCB copper layer count")
    parser.add_argument("--pcb-profile", help="pcb-design-skill profile name")
    parser.add_argument("--pcb-skill", help="path to the pcb-design-skill directory")
    parser.add_argument("--erc", action="store_true",
                        help="also run KiCad ERC when kicad-cli is compatible")
    parser.add_argument("--require", metavar="STAGES",
                        help="comma-separated stages that must PASS: design, limits, measurements, models, simulate, erc, pcb-drc, kicad-parse")
    ns = parser.parse_args(argv)
    required = [s.strip() for s in (ns.require or "").split(",") if s.strip()]
    unknown = [s for s in required if s not in REQUIRED_CHECK]
    if unknown:
        parser.error("unknown stage(s) in --require: " + ", ".join(unknown)
                     + " (allowed: " + ", ".join(sorted(REQUIRED_CHECK)) + ")")
    try:
        result = verify(
            ns.spec, ns.out, update_report=not ns.no_update_report,
            pcb_board=ns.pcb_board, pcb_profile=ns.pcb_profile,
            pcb_layers=ns.pcb_layers, pcb_skill=ns.pcb_skill,
            erc=ns.erc, required=required,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if ns.json:
        print(dumps(result))
    else:
        _print(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
