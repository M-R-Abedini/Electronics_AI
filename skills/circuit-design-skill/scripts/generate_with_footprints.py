#!/usr/bin/env python3
"""Prompt for an assembly style, assign conservative common KiCad footprints,
then delegate all artifact generation to generate.py.

Existing per-component footprint choices are never overwritten. Packages whose
size or pinout cannot be inferred safely are deliberately left unassigned.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

# KiCad library identifiers are deliberately conservative defaults.
FOOTPRINTS = {
    "smd": {
        "R": "Resistor_SMD:R_0805_2012Metric",
        "C": "Capacitor_SMD:C_0805_2012Metric",
        "L": "Inductor_SMD:L_0805_2012Metric",
    },
    "through-hole": {
        "R": "Resistor_THT:R_Axial_DIN0309_L9.0mm_D3.2mm_P12.70mm_Horizontal",
        "C": "Capacitor_THT:C_Disc_D5.0mm_W2.5mm_P5.00mm",
        "L": "Inductor_THT:L_Radial_D10.0mm_P5.00mm",
    },
}


# Never infer semiconductor packages from device class. Research exact MPN options.
SEMICONDUCTOR_TYPES = {
    "D", "LED", "ZENER", "NPN", "PNP", "NMOS", "PMOS", "OPAMP",
    "IC", "BJT", "MOSFET", "JFET", "IGBT", "SCR", "TRIAC", "TVS",
    "SCHOTTKY", "RECTIFIER", "BRIDGE", "REGULATOR", "MCU", "ADC", "DAC",
}


def semiconductor_refs_requiring_research(spec: dict[str, Any]) -> list[str]:
    return [str(c.get("ref", "")) for c in spec.get("components", [])
            if c.get("type") in SEMICONDUCTOR_TYPES and not c.get("footprint")]


def assign_common_footprints(spec: dict[str, Any], assembly: str) -> dict[str, str]:
    """Fill missing component footprints; return ref -> assigned footprint."""
    if assembly not in FOOTPRINTS:
        raise ValueError("assembly must be 'smd' or 'through-hole'")
    defaults = FOOTPRINTS[assembly]
    assigned: dict[str, str] = {}
    for comp in spec.get("components", []):
        ref = str(comp.get("ref", ""))
        if comp.get("footprint") or comp.get("type") not in defaults:
            continue
        footprint = defaults[comp["type"]]
        comp["footprint"] = footprint
        comp["footprint_status"] = "provisional"
        comp["footprint_evidence"] = (
            f"Generic {assembly} passive default; confirm dimensions against selected MPN."
        )
        comp["pad_map_verified"] = False
        assigned[ref] = footprint
    return assigned


def _needs_footprints(spec_path: Path, args: list[str]) -> bool:
    # Explicit PCB options make this a PCB-generation run even when the spec's
    # deliverables list does not include "pcb".
    if any(arg == option or arg.startswith(option + "=")
           for arg in args
           for option in ("--pcb-board", "--pcb-profile", "--pcb-layers")):
        return True
    requested = None
    for index, arg in enumerate(args):
        if arg == "--formats" and index + 1 < len(args):
            requested = [x.strip() for x in args[index + 1].split(",") if x.strip()]
        elif arg.startswith("--formats="):
            requested = [x.strip() for x in arg.split("=", 1)[1].split(",") if x.strip()]
    if requested is not None:
        return bool({"kicad_sch", "pcb"} & set(requested))
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    return bool({"kicad_sch", "pcb"} & set(spec.get("deliverables", [])))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--assembly", choices=("smd", "through-hole"))
    options, passthrough = parser.parse_known_args(argv)
    if "-h" in passthrough or "--help" in passthrough:
        from generate import main as generate_main
        sys.argv = ["generate.py", *passthrough]
        return generate_main()
    if not passthrough:
        print("error: provide a design spec path", file=sys.stderr)
        return 2

    spec_path = Path(passthrough[0]).resolve()
    if not _needs_footprints(spec_path, passthrough):
        from generate import main as generate_main
        sys.argv = ["generate.py", *passthrough]
        return generate_main()

    assembly = options.assembly
    if assembly is None:
        if not sys.stdin.isatty():
            print(
                "error: choose the primary assembly style with "
                "--assembly smd or --assembly through-hole",
                file=sys.stderr,
            )
            return 2
        print("What is the primary assembly style?")
        print("  1) SMD (surface-mount)")
        print("  2) Through-hole (preferred default)")
        answer = input("Select [1/2, default 2]: ").strip().lower() or "2"
        choices = {"1": "smd", "s": "smd", "smd": "smd",
                   "2": "through-hole", "t": "through-hole",
                   "th": "through-hole", "through-hole": "through-hole"}
        assembly = choices.get(answer)
        if assembly is None:
            print("error: enter 1 for SMD or 2 for through-hole", file=sys.stderr)
            return 2

    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    semiconductor_todo = semiconductor_refs_requiring_research(spec)
    assigned = assign_common_footprints(spec, assembly)
    if assigned:
        print(f"Assembly profile: {assembly}")
        print("Auto-assigned common footprints:")
        for ref, footprint in assigned.items():
            print(f"  {ref}: {footprint}")
    else:
        print(f"Assembly profile: {assembly}; no safe default assignments found.")

    if semiconductor_todo:
        print("Semiconductor footprints require manufacturer-package research:")
        for ref in semiconductor_todo:
            print(f"  {ref}: research exact MPN package options; prefer DIY-friendly through-hole")

    # Keep the temporary spec beside the original so relative model paths remain valid.
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".json",
        prefix=".footprints-", dir=spec_path.parent, delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        json.dump(spec, handle, indent=2)
        handle.write("\n")
    try:
        passthrough[0] = str(temp_path)
        from generate import main as generate_main
        sys.argv = ["generate.py", *passthrough]
        result = generate_main()
        if assigned:
            print(
                "Note: inferred passive footprints are provisional defaults only. "
                "Confirm package dimensions against the selected MPN before layout."
            )
        return result
    finally:
        temp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
