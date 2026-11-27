# SPDX-License-Identifier: MIT
"""Collect reproducible PCB placement, routing and visual-review evidence on board copies."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from eda_agent import placer
from eda_agent.placer import corpus, report
from eda_agent.placer.io.kicad import SIDECARS, read_board
from eda_agent.placer.toolchain import sha256_file

SEEDS = (0, 7)


def _write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=1, ensure_ascii=False), encoding="utf-8")


def inventory(out):
    from eda_agent.placer.place.mechanical import interface_inventory

    sealed = {row.path.resolve() for row in corpus.entries("test")}
    rows = []
    for path in sorted(corpus.EXAMPLES.rglob("*")):
        suffix = path.suffix.lower()
        if suffix not in (".kicad_pcb", ".pcbdoc") or not path.is_file():
            continue
        row = {"file": str(path), "sha256": sha256_file(path)}
        if suffix == ".pcbdoc":
            row["status"] = "altium_inventory"
        elif path.resolve() in sealed:
            row["status"] = "sealed"
        else:
            try:
                board = read_board(path)
                row.update(status="read", footprints=len(board.footprints), outline=board.outline_valid,
                           legacy=board.source.get("upgraded_from_legacy", False), zones=len(board.zones),
                           interfaces=interface_inventory(board))
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                row.update(status="read_error", reason=str(exc))
        rows.append(row)
        print(row["status"], path.name, flush=True)
    _write(out / "inventory.json", rows)


def _engine_source():
    root = Path(placer.__file__).resolve().parent
    repo = root.parents[3]
    digest = hashlib.sha256()
    for path in sorted(path for path in root.rglob("*") if path.suffix in (".py", ".json")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(bytes.fromhex(sha256_file(path)))
    rust_root = repo / "crates" / "placer-core"
    if rust_root.is_dir():
        for path in sorted(p for p in rust_root.rglob("*") if p.suffix in (".rs", ".toml")):
            if "target" in path.parts:
                continue
            digest.update(path.relative_to(repo).as_posix().encode())
            digest.update(bytes.fromhex(sha256_file(path)))
    return {"path": str(root), "tree_sha256": digest.hexdigest(),
            "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()}


def benchmark(out, group, names, seeds):
    from eda_agent.placer.place.pipeline import place_board

    root = corpus.EXAMPLES
    cases = {
        "luckfox": root / "LuckFox-Core3506-Carrier" / "luckfox_core3506_adapter.kicad_pcb",
        "granit": root / "granit" / "granit.kicad_pcb",
        "urti": corpus.get("urti_mainboard").path,
        "mppt": root / "mppt-2420-lc" / "MPPT_charger_20A.kicad_pcb",
        "esp32_poe": corpus.get("esp32_poe_m1").path,
        "dcdc": next(root.glob("DCDC-50-5-12-main/**/DCDC-50-5-12_Rev_A1.kicad_pcb")),
        "hackrf": corpus.get("hackrf").path,
    } if group == "placement" else {
        "pic_programmer": corpus.get("pic_programmer").path,
        "ecc83": corpus.get("ecc83").path,
        "granit": root / "granit" / "granit.kicad_pcb",
    }
    if names:
        cases = {name: cases[name] for name in names}
    options = {
        name: {"mode": "sa_only" if name in ("urti", "mppt") else "best",
               "floorplan": name not in ("urti", "mppt"), "route": group == "routing",
               "router_timeout_s": (90 if name == "granit" else 180) if group == "routing" else None}
        for name in cases
    }
    snapshot = {"source": _engine_source(), "tools": report.tools(), "jobs": {
        name: {"input": str(path), "sha256": sha256_file(path), "options": options[name], "seeds": seeds,
               "sidecars": {suffix: sha256_file(path.with_suffix(suffix))
                            for suffix in SIDECARS if path.with_suffix(suffix).is_file()}}
        for name, path in cases.items()
    }}
    _write(out / "manifest.json", snapshot)
    for name, path in cases.items():
        for seed in seeds:
            target = out / f"{name}-{seed}"
            try:
                result = place_board(path, out_dir=target, seed=seed, **options[name])
                print(name, seed, result["passed"],
                      {stage: value["status"] for stage, value in result.get("quality", {}).items()}, flush=True)
            except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
                _write(target / "failure.json", {"type": type(exc).__name__, "message": str(exc)})
                print(name, seed, type(exc).__name__, str(exc), flush=True)


def repair(out, runs):
    from eda_agent.placer import quality, silkscreen
    from eda_agent.placer.intent.resolve import resolve
    from eda_agent.placer.io.kicad import write_board
    from eda_agent.placer.place import problem, review

    _write(out / "manifest.json", {"source": _engine_source(), "tools": report.tools()})
    for location in runs:
        original = json.loads(location.read_text(encoding="utf-8"))
        source = Path(original["inputs"]["board"]["path"])
        candidate = Path(original["artifacts"]["placed_board"])
        basis = read_board(source)
        saved_intent = original["inputs"].get("intent") or {}
        intent, _ = resolve(basis, saved_intent.get("path"))
        reviewer = quality.PlacementReview(basis, intent, problem.fixed_parts(basis, intent))

        def write_trial(board, path, *, source=source, intent=intent):
            stats = {}
            cfg = silkscreen.build_config(board, intent)
            artifact = write_board(source, board, path, strip_routing=True, unfill_zones=True,
                                   silkscreen_config=cfg, result_out=stats)
            return artifact, stats

        root = out / source.stem
        _, _, checks = review.run(candidate, reviewer, root, write_trial)
        _write(root / "review.json", checks)
        print(source.stem, checks["status"], checks["stop"], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group", choices=("placement", "routing", "inventory", "repair"))
    parser.add_argument("out", type=Path)
    parser.add_argument("--boards", nargs="+")
    parser.add_argument("--seeds", nargs="+", type=int, default=SEEDS)
    parser.add_argument("--legacy-run", type=Path, nargs="+")
    args = parser.parse_args()
    if args.group == "inventory":
        inventory(args.out)
    elif args.group == "repair":
        if not args.legacy_run:
            parser.error("repair requires --legacy-run")
        repair(args.out, args.legacy_run)
    else:
        benchmark(args.out, args.group, args.boards, args.seeds)


if __name__ == "__main__":
    main()
