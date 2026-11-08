# SPDX-License-Identifier: MIT
"""Run Cypress (NVlabs/Cypress, Apache-2.0) on boards exported by ``pixi run cypress export``.

Runs inside the Cypress environment (its Docker image or a DREAMPlace build).
For every ``*.kicad_pcb`` in --in it uses Cypress's own converters from
``pcb-util`` (KiCadConverter -> BookshelfConverter), runs ``Placer.py`` with the
defaults of Cypress's PCB tuning space, and writes back:

  <out>/<name>.cypress.kicad_pcb   placed board (Cypress's KiCad writer)
  <out>/<name>.pl                  raw placement (fallback for the importer)
  <out>/<name>.log                 everything Cypress printed
  <out>/summary.json               status, seconds and versions per board

Nothing here talks to the network; the boards stay local.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time
import traceback

# defaults of test/tune/pcb-configspace.json in the Cypress repository
PCB_DEFAULTS = {
    "num_bins_x": 512,
    "num_bins_y": 512,
    "density_weight": 0.008,
    "gamma": 0.1318231577,
    "target_density": 1.0,
    "stop_overflow": 0.07,
}


def find_placer(root: str) -> str:
    hits = sorted(glob.glob(os.path.join(root, "**", "dreamplace", "Placer.py"), recursive=True),
                  key=lambda p: ("install" not in p, len(p)))
    if not hits:
        raise FileNotFoundError(f"dreamplace/Placer.py not found under {root}; build Cypress first")
    return hits[0]


def git_head(root: str) -> str:
    try:
        return subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def run_one(kicad: str, out_dir: str, root: str, placer: str, timeout_s: int) -> dict:
    sys.path.insert(0, os.path.join(root, "pcb-util"))
    from bookshelf_converter import BookshelfConverter
    from kicad_converter import KiCadConverter

    name = os.path.basename(kicad).removesuffix(".kicad_pcb")
    work = os.path.join(out_dir, "work", name)
    bs = os.path.join(work, "bookshelf")
    os.makedirs(bs, exist_ok=True)
    row = {"name": name, "status": "started"}
    t0 = time.time()
    pcb = KiCadConverter(target_path=kicad).from_kicad()
    BookshelfConverter(design_name=name, target_folder=bs, filter_fixed_on_layer=None,
                       movable_pages=None).to_bookshelf(pcb)
    aux = glob.glob(os.path.join(bs, "*.aux"))
    if not aux:
        raise FileNotFoundError("Cypress's converter wrote no .aux")
    with open(os.path.join(root, "test", "simple.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg.update(PCB_DEFAULTS)
    cfg["aux_input"] = aux[0]
    cfg["gpu"] = 1
    cfg["result_dir"] = os.path.join(work, "results")
    cfg["opt_num_bins"] = [dict(cfg["opt_num_bins"][0], x=PCB_DEFAULTS["num_bins_x"], y=PCB_DEFAULTS["num_bins_y"])]
    cfg_path = os.path.join(work, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=1)
    with open(os.path.join(out_dir, f"{name}.log"), "w", encoding="utf-8") as log:
        done = subprocess.run([sys.executable, placer, cfg_path], cwd=os.path.dirname(os.path.dirname(placer)),
                              stdout=log, stderr=subprocess.STDOUT, timeout=timeout_s, check=False)
    row["placer_exit"] = done.returncode
    pls = sorted(glob.glob(os.path.join(work, "results", "**", "*.pl"), recursive=True), key=os.path.getmtime)
    if not pls:
        raise FileNotFoundError("Placer.py wrote no .pl (see the log)")
    best = next((p for p in reversed(pls) if ".lg." in p), pls[-1])
    shutil.copyfile(best, os.path.join(out_dir, f"{name}.pl"))
    shutil.copyfile(best, os.path.join(bs, f"{name}.pl"))
    row["pl"] = os.path.basename(best)
    try:
        placed = BookshelfConverter(design_name=name, target_folder=bs, filter_fixed_on_layer=None,
                                    movable_pages=None).from_bookshelf()
        KiCadConverter(target_path=os.path.join(out_dir, f"{name}.cypress.kicad_pcb")).to_kicad(placed)
        row["kicad_written"] = True
    except Exception as exc:  # noqa: BLE001  # the raw .pl is still there for the importer
        row["kicad_written"] = False
        row["kicad_error"] = f"{type(exc).__name__}: {exc}"
    row["seconds"] = round(time.time() - t0, 1)
    row["status"] = "ok"
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cypress", default="/Cypress")
    ap.add_argument("--timeout", type=int, default=3600)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    placer = find_placer(args.cypress)
    summary = {"cypress_commit": git_head(args.cypress), "placer": placer, "python": sys.version, "boards": []}
    try:
        import torch

        summary["torch"] = torch.__version__
        summary["cuda"] = torch.version.cuda
        summary["gpu"] = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
    except Exception as exc:  # noqa: BLE001
        summary["torch"] = f"unavailable: {exc}"
    for kicad in sorted(glob.glob(os.path.join(args.inp, "*.kicad_pcb"))):
        try:
            row = run_one(kicad, args.out, args.cypress, placer, args.timeout)
        except Exception as exc:  # noqa: BLE001
            row = {"name": os.path.basename(kicad), "status": "failed", "error": f"{type(exc).__name__}: {exc}",
                   "trace": traceback.format_exc()[-2000:]}
        summary["boards"].append(row)
        print(json.dumps({k: row.get(k) for k in ("name", "status", "seconds", "kicad_written", "error")}), flush=True)
        with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=1)
    return 0 if all(b["status"] == "ok" for b in summary["boards"]) else 1


if __name__ == "__main__":
    sys.exit(main())
