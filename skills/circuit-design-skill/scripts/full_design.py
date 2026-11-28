#!/usr/bin/env python3
"""Run circuit generation and the sibling KiCad PCB pipeline end to end."""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def find_pcb_skill(explicit: str | None) -> Path | None:
    candidates = [explicit, os.environ.get("PCB_DESIGN_SKILL")]
    candidates.extend(str(parent / "pcb-design-skill") for parent in ROOT.parents)
    for candidate in candidates:
        if candidate:
            path = Path(candidate).expanduser().resolve()
            if (path / "scripts" / "design_pipeline.py").is_file():
                return path
    return None

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a circuit, then build, route, check and export its KiCad PCB.")
    parser.add_argument("spec", help="normalized circuit design JSON")
    parser.add_argument("--out", default="out/full-design", help="output root")
    parser.add_argument("--pcb-skill", help="path to sibling pcb-design-skill")
    parser.add_argument("--board", required=True, help="board size in mm, WIDTHxHEIGHT")
    parser.add_argument("--layers", type=int, default=2, choices=(2, 4, 6))
    parser.add_argument("--profile", default="2layer-low-speed")
    parser.add_argument("--assembly", required=True, choices=("through-hole", "smd"))
    parser.add_argument("--skip-route", action="store_true",
                        help="skip Freerouting; still build, check and export the native board")
    parser.add_argument("--backend", choices=("local",), default="local",
                        help="PCB integration runs locally; sibling CI cannot access temporary handoff files")
    parser.add_argument("--ask", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--ask-mobile", action="store_true")
    parser.add_argument("--allow-unverified", action="store_true",
                        help="return success for incomplete PCB gates; report still records failures")
    args = parser.parse_args(argv)

    pcb_skill = find_pcb_skill(args.pcb_skill)
    if pcb_skill is None:
        parser.error("pcb-design-skill not found; pass --pcb-skill or set PCB_DESIGN_SKILL")
    if args.layers != 2 and args.profile == "2layer-low-speed":
        parser.error("choose a profile compatible with the requested layer count")
    if args.ask_mobile and args.ask is False:
        parser.error("--ask-mobile cannot be combined with --no-ask")

    out = Path(args.out).resolve()
    generated, pcb_out = out / "generated", out / "pcb"
    generated.mkdir(parents=True, exist_ok=True)
    pcb_out.mkdir(parents=True, exist_ok=True)
    try:
        spec_data = json.loads(Path(args.spec).read_text(encoding="utf-8"))
        stem = str(spec_data["name"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        parser.error(f"cannot read design spec name: {exc}")

    generator = [
        sys.executable, str(ROOT / "scripts" / "generate_with_footprints.py"),
        str(Path(args.spec).resolve()), "--assembly", args.assembly,
        "--out", str(generated), "--formats",
        "spice,ltspice_asc,kicad_net,kicad_sch,bom,report,pcb",
        "--pcb-board", args.board, "--pcb-profile", args.profile,
        "--pcb-layers", str(args.layers), "--pcb-skill", str(pcb_skill),
    ]
    print("[1/2] Generate schematic, SPICE, BOM and PCB handoff", flush=True)
    result = subprocess.run(generator, cwd=ROOT)
    if result.returncode:
        return result.returncode

    overlay = generated / f"{stem}-pcb.json"
    spice = generated / f"{stem}.cir"
    pin_map = generated / f"{stem}-pinmap.json"
    aliases = generated / f"{stem}-node-aliases.json"
    missing = [str(p) for p in (overlay, spice, pin_map, aliases) if not p.is_file()]
    if missing:
        print("error: generator did not produce required PCB inputs:", file=sys.stderr)
        for path in missing:
            print(f"  {path}", file=sys.stderr)
        return 1

    pipeline = [
        sys.executable, str(pcb_skill / "scripts" / "design_pipeline.py"),
        "all", str(overlay), "--ltspice", str(spice),
        "--pin-map", str(pin_map), "--node-aliases", str(aliases),
        "--output", str(pcb_out), "--backend", "local",
    ]
    if args.ask is not None:
        pipeline.append("--ask" if args.ask else "--no-ask")
    if args.ask_mobile:
        pipeline.append("--ask-mobile")
    if args.skip_route:
        pipeline.append("--skip-route")
    if args.allow_unverified:
        pipeline.append("--allow-unverified")

    print("[2/2] Build, route, run DRC/DFM and export fabrication files", flush=True)
    result = subprocess.run(pipeline, cwd=pcb_skill)
    if result.returncode:
        print(
            f"PCB release gates did not pass (exit {result.returncode}). "
            f"Inspect {pcb_out / 'report' / 'design-report.md'}; circuit artifacts "
            f"remain in {generated}.",
            file=sys.stderr,
        )
        return result.returncode

    print(f"PCB pipeline completed. Inspect {pcb_out / (stem + '.kicad_pcb')} "
          f"and {pcb_out / (stem + '.kicad_pro')}")
    print(f"Full output: {out}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
