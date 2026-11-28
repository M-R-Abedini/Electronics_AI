#!/usr/bin/env python3
"""Render a generated KiCad schematic to SVG using the official KiCad CLI."""
from __future__ import annotations

import argparse
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def render(schematic: Path, output_dir: Path, cli: str = "kicad-cli") -> list[Path]:
    if not schematic.is_file():
        raise FileNotFoundError(f"KiCad schematic not found: {schematic}")
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [cli, "sch", "export", "svg", "--output", str(output_dir), str(schematic)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"{cli!r} was not found; install KiCad 9+ or provide --cli"
        ) from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"KiCad SVG export failed (exit {result.returncode}): {detail}")

    files = sorted(output_dir.glob("*.svg"))
    if not files:
        raise RuntimeError("KiCad reported success but produced no SVG files")
    for path in files:
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError) as exc:
            raise RuntimeError(f"KiCad produced invalid SVG {path}: {exc}") from exc
        if not root.tag.endswith("svg"):
            raise RuntimeError(f"KiCad output is not an SVG document: {path}")
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("schematic", type=Path, help="generated .kicad_sch file")
    parser.add_argument("--out", type=Path, required=True, help="SVG output directory")
    parser.add_argument("--cli", default="kicad-cli", help="KiCad CLI executable")
    args = parser.parse_args(argv)
    try:
        files = render(args.schematic, args.out, args.cli)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"schematic render unavailable: {exc}", file=sys.stderr)
        return 2
    for path in files:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
