# SPDX-License-Identifier: MIT
"""Place every board of /content/in with Cypress on the GPU; write /content/out/*.placed.json.

Input: ``<name>.json`` in the dictionary form of Cypress's ``pcb.PCB`` plus a
``fixed`` list (written by ``pixi run cypress export``).  The board goes through
Cypress's own BookshelfConverter with ``anonymous=False`` (the default would
replace reference designators), ``dreamplace/Placer.py`` places it on the GPU,
and the placed design is written back as a ``PCB.to_dict()``.
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import time
import traceback

CYPRESS = os.environ.get("CYPRESS_DIR", "/content/Cypress")
INSTALL = os.path.join(CYPRESS, "install")
IN_DIR = os.environ.get("CYPRESS_IN", "/content/in")
OUT_DIR = os.environ.get("CYPRESS_OUT", "/content/out")
TIMEOUT_S = int(os.environ.get("CYPRESS_TIMEOUT", "3600"))
# defaults of test/tune/pcb-configspace.json in the Cypress repository
PCB_DEFAULTS = {"num_bins_x": 512, "num_bins_y": 512, "density_weight": 0.008, "gamma": 0.1318231577,
                "target_density": 1.0, "stop_overflow": 0.07}

sys.path.insert(0, os.path.join(CYPRESS, "pcb-util"))
from bookshelf_converter import BookshelfConverter
from pcb import PCB


def load_pcb(data):
    pcb = PCB()
    result = pcb.from_dict({k: v for k, v in data.items() if k != "fixed"})
    pcb = result if isinstance(result, PCB) else pcb
    fixed = set(data.get("fixed", []))
    for name, comp in pcb.components.items():
        comp.fixed = name in fixed
    return pcb


def run_one(path):
    name = os.path.basename(path)[:-5]
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    work = os.path.join(OUT_DIR, "work", name)
    bs = os.path.join(work, "bookshelf")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(bs)
    row = {"name": name, "components": len(data["components"]), "fixed": len(data.get("fixed", []))}
    t0 = time.time()
    pcb = load_pcb(data)
    BookshelfConverter(design_name=name, target_folder=bs, filter_fixed_on_layer=None, movable_pages=None,
                       anonymous=False).to_bookshelf(pcb)
    pl_in = os.path.join(bs, f"{name}.pl")
    with open(pl_in, encoding="utf-8") as fh:
        row["fixed_in_bookshelf"] = sum(1 for line in fh if "/FIXED" in line)
    shutil.copyfile(pl_in, pl_in + ".orig")
    with open(os.path.join(CYPRESS, "test", "simple.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg.update(PCB_DEFAULTS)
    cfg.update({"aux_input": os.path.join(bs, f"{name}.aux"), "gpu": 1, "plot_flag": 0,
                "result_dir": os.path.join(work, "results"),
                "opt_num_bins": [dict(cfg["opt_num_bins"][0], x=PCB_DEFAULTS["num_bins_x"],
                                      y=PCB_DEFAULTS["num_bins_y"])]})
    cfg_path = os.path.join(work, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=1)
    log_path = os.path.join(OUT_DIR, f"{name}.log")
    with open(log_path, "w", encoding="utf-8") as log:
        done = subprocess.run([sys.executable, os.path.join(INSTALL, "dreamplace", "Placer.py"), cfg_path],
                              cwd=INSTALL, stdout=log, stderr=subprocess.STDOUT, timeout=TIMEOUT_S, check=False)
    row["placer_exit"] = done.returncode
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    row["log_tail"] = text[-1500:]
    row["gpu_mentioned"] = ("gpu" in text.lower()) or ("cuda" in text.lower())
    pls = sorted(glob.glob(os.path.join(work, "results", "**", "*.pl"), recursive=True), key=os.path.getmtime)
    if done.returncode != 0 or not pls:
        raise RuntimeError(f"Placer.py exit {done.returncode}, {len(pls)} .pl files; see {name}.log")
    best = next((p for p in reversed(pls) if ".lg." in os.path.basename(p)), pls[-1])
    row["pl"] = os.path.relpath(best, work)
    shutil.copyfile(best, pl_in)
    placed = BookshelfConverter(design_name=name, target_folder=bs, filter_fixed_on_layer=None, movable_pages=None,
                                anonymous=False).from_bookshelf()
    with open(os.path.join(OUT_DIR, f"{name}.placed.json"), "w", encoding="utf-8") as fh:
        json.dump(placed.to_dict(), fh)
    shutil.copyfile(best, os.path.join(OUT_DIR, f"{name}.pl"))
    row["seconds"] = round(time.time() - t0, 1)
    row["status"] = "ok"
    return row


def main():
    import torch

    os.makedirs(OUT_DIR, exist_ok=True)
    summary = {"torch": torch.__version__, "torch_cuda": torch.version.cuda,
               "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
               "cypress_commit": subprocess.run(["git", "-C", CYPRESS, "rev-parse", "HEAD"], capture_output=True, check=False,
                                                text=True).stdout.strip(),
               "boards": []}
    if not torch.cuda.is_available():
        raise SystemExit("CUDA is not available to PyTorch: refusing to run Cypress on the CPU")
    for path in sorted(glob.glob(os.path.join(IN_DIR, "*.json"))):
        if os.path.basename(path) == "manifest.json":
            continue
        try:
            row = run_one(path)
        except Exception as exc:  # noqa: BLE001
            row = {"name": os.path.basename(path), "status": "failed", "error": f"{type(exc).__name__}: {exc}",
                   "trace": traceback.format_exc()[-3000:]}
        summary["boards"].append(row)
        print(json.dumps({k: row.get(k) for k in ("name", "status", "seconds", "fixed", "fixed_in_bookshelf",
                                                  "gpu_mentioned", "error")}), flush=True)
        with open(os.path.join(OUT_DIR, "summary.json"), "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=1)


if __name__ == "__main__":
    main()
