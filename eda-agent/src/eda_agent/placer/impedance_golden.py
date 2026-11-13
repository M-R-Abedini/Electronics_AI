# SPDX-License-Identifier: MIT
"""Polar SI9000 golden set for the impedance solver (plan F3, task 6).

``pixi run impedance cases`` writes the geometry list (blind: no predicted
values).  The user types each row into Polar SI9000 and fills ``polar_z0`` or
``polar_zdiff``.  ``pixi run impedance compare <csv>`` then scores the solver:
gate <= 5 % single-ended and <= 8 % differential over the set.

The rows cover the corpus stackups: 2-layer 1.51 mm cores, 4-layer 0.1 / 0.2 /
0.48 mm dielectrics, the 8-layer royalblue inner layers, mask 10 and 25 um,
and the net-class widths and gaps of the dev and validation boards.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

from .impedance import Geometry, closed, solve

GOLDEN = Path(__file__).resolve().parents[3] / "tests" / "placer" / "golden" / "impedance"
SE_GATE, DIFF_GATE = 0.05, 0.08

POLAR_NAMES = {
    "surface_microstrip": "Surface Microstrip 1B",
    "coated_microstrip": "Coated Microstrip 1B",
    "offset_stripline": "Offset Stripline 1B1A",
    "edge_coupled_surface_microstrip": "Edge Coupled Surface Microstrip 1B",
    "edge_coupled_coated_microstrip": "Edge Coupled Coated Microstrip 1B",
    "edge_coupled_offset_stripline": "Edge Coupled Offset Stripline 1B1A",
}
FIELDS = ("id", "polar_structure", "H1", "H2", "W1", "W2", "T1", "S1", "Er1", "Er2", "C1", "C2", "C3", "CEr",
          "polar_z0", "polar_zdiff", "note")


def cases() -> list[tuple[str, Geometry, str]]:
    out: list[tuple[str, Geometry, str]] = []
    sm = [(0.1, 0.1, "8L/4L thin prepreg"), (0.1, 0.2, ""), (0.2, 0.3, "~50 ohm"), (0.2, 0.15, ""),
          (0.48, 0.2, "video/coldfire 4L"), (0.48, 0.9, "~50 ohm"), (1.51, 0.15, "StickHub 2L min track"),
          (1.51, 0.5, "pic/ecc83 2L"), (1.51, 2.8, "~50 ohm on 2L")]
    for h, w, note in sm:
        out.append(("SM", Geometry("surface_microstrip", H1=h, W1=w), note))
    for h, w, c, note in [(0.1, 0.2, 0.01, "mask 10 um"), (0.2, 0.3, 0.01, ""), (0.48, 0.2, 0.01, ""),
                          (1.51, 0.5, 0.01, ""), (1.51, 2.8, 0.01, ""), (0.2, 0.3, 0.025, "mask 25 um"),
                          (1.51, 0.15, 0.025, "")]:
        out.append(("CM", Geometry("coated_microstrip", H1=h, W1=w, C1=c, C2=c), note))
    for h1, h2, w, note in [(0.1, 0.335, 0.1, "royalblue In-layer: 0.1 prepreg below, 0.3 core above"),
                            (0.1, 0.335, 0.15, ""), (0.2, 0.235, 0.15, "symmetric"),
                            (0.48, 0.515, 0.2, "symmetric 4L inner")]:
        out.append(("OS", Geometry("offset_stripline", H1=h1, H2=h2, W1=w), note))
    for h, w, s, note in [(0.1, 0.125, 0.2032, "royalblue USB_DIFF"), (0.1, 0.1, 0.15, ""),
                          (0.2, 0.15, 0.15, ""), (0.48, 0.2, 0.25, "4L default diff"),
                          (1.51, 0.15, 0.15, "StickHub USB"), (1.51, 0.5, 0.2, "")]:
        out.append(("ECSM", Geometry("edge_coupled_surface_microstrip", H1=h, W1=w, S1=s), note))
    for h, w, s, c, note in [(0.1, 0.125, 0.2032, 0.01, "royalblue USB_DIFF with mask"),
                             (1.51, 0.15, 0.15, 0.01, "StickHub USB with mask"),
                             (0.48, 0.2, 0.25, 0.01, ""), (0.2, 0.15, 0.15, 0.025, "mask 25 um")]:
        out.append(("ECCM", Geometry("edge_coupled_coated_microstrip", H1=h, W1=w, S1=s, C1=c, C2=c, C3=c), note))
    for h1, h2, w, s, note in [(0.1, 0.335, 0.1, 0.15, "8L inner pair"), (0.2, 0.235, 0.125, 0.2, "symmetric")]:
        out.append(("ECOS", Geometry("edge_coupled_offset_stripline", H1=h1, H2=h2, W1=w, S1=s), note))
    numbered, seen = [], {}
    for prefix, g, note in out:
        seen[prefix] = seen.get(prefix, 0) + 1
        numbered.append((f"{prefix}{seen[prefix]:02d}", g, note))
    return numbered


def _row(cid: str, g: Geometry, note: str) -> dict:
    def f(v):
        return "" if v is None else f"{v:g}"
    return {"id": cid, "polar_structure": POLAR_NAMES[g.structure], "H1": f(g.H1), "H2": f(g.H2), "W1": f(g.W1),
            "W2": f(g.W1), "T1": f(g.T1), "S1": f(g.S1), "Er1": f(g.Er1),
            "Er2": f(g.Er2 if g.Er2 is not None else (g.Er1 if g.stripline else None)),
            "C1": f(g.C1) if g.coated else "", "C2": f(g.C2) if g.coated else "",
            "C3": f(g.C3) if g.coated and g.coupled else "", "CEr": f(g.CEr) if g.coated else "",
            "polar_z0": "", "polar_zdiff": "", "note": note}


def write_cases(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:  # BOM: Excel opens it as UTF-8
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for cid, g, note in cases():
            w.writerow(_row(cid, g, note))
    return path


def compare(path: Path) -> dict:
    geoms = {cid: (g, _row(cid, g, note)) for cid, g, note in cases()}
    rows = []
    seen = set()
    with path.open(encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not set(FIELDS[:-1]).issubset(reader.fieldnames or ()):
            raise ValueError("Polar CSV is missing required case columns")
        for r in reader:
            cid = r["id"]
            if cid not in geoms or cid in seen:
                raise ValueError(f"unknown or duplicate Polar case: {cid!r}")
            seen.add(cid)
            g, expected = geoms[cid]
            key = "polar_zdiff" if g.coupled else "polar_z0"
            if not (r.get(key) or "").strip():
                continue
            if r["polar_structure"] != expected["polar_structure"] or any(
                (float(r[field].replace(",", ".")) if r[field] else None) != (float(expected[field]) if expected[field] else None)
                for field in FIELDS[2:-3]
            ):
                raise ValueError(f"{cid}: measured geometry differs from the reference case")
            polar = float(r[key].replace(",", "."))
            if not math.isfinite(polar) or polar <= 0:
                raise ValueError(f"{cid}: Polar impedance must be finite and positive")
            res = solve(g)
            ours = res["zdiff"] if g.coupled else res["z0"]
            rows.append({"id": r["id"], "structure": g.structure, "polar": polar, "field": ours,
                         "closed": closed(g), "error": (ours - polar) / polar, "diff": g.coupled})
    se = [abs(r["error"]) for r in rows if not r["diff"]]
    df = [abs(r["error"]) for r in rows if r["diff"]]
    missing = sorted(set(geoms) - {r["id"] for r in rows})
    gates = [{"name": "polar_case_coverage", "value": len(rows), "op": "==", "threshold": len(geoms),
              "passed": not missing},
             {"name": "single_ended_max_error", "value": max(se) if se else None, "op": "<=", "threshold": SE_GATE,
              "passed": bool(se) and max(se) <= SE_GATE},
             {"name": "differential_max_error", "value": max(df) if df else None, "op": "<=", "threshold": DIFF_GATE,
              "passed": bool(df) and max(df) <= DIFF_GATE}]
    return {"rows": rows, "compared": len(rows), "of": len(geoms), "missing_cases": missing, "gates": gates}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="impedance", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cases")
    c.add_argument("--out", default=str(GOLDEN / "polar_cases.csv"))
    m = sub.add_parser("compare")
    m.add_argument("csv")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    if args.cmd == "cases":
        print(write_cases(Path(args.out)))
        return 0
    rep = compare(Path(args.csv))
    print(json.dumps(rep, indent=1))
    return 0 if all(g["passed"] for g in rep["gates"]) else 1


if __name__ == "__main__":
    sys.exit(main())
