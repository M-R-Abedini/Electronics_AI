# SPDX-License-Identifier: MIT
"""Generate scripts/cypress/cypress_colab.ipynb (run once after editing cypress_run.py)."""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def md(text):
    return {"cell_type": "markdown", "metadata": {}, "source": text.strip("\n").splitlines(keepends=True)}


def code(text):
    return {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
            "source": text.strip("\n").splitlines(keepends=True)}


DRIVER = (HERE / "cypress_run.py").read_text(encoding="utf-8")

cells = [
    md("""
# Cypress baseline for Electronics AI (plan F1)

1. **Runtime > Change runtime type > A100 GPU**, then save.
2. **Runtime > Run all**. Cell 2 asks for `cypress_in.zip` (from `pixi run cypress export`).
3. When the last cell finishes, `cypress_out.zip` downloads. Send it back.

The build pins the stack of Cypress's own Dockerfile (nvcr.io/nvidia/pytorch:21.10): Python 3.8,
PyTorch 1.10.0 + CUDA 11.3, nvcc 11.3 with gcc 10. Newer stacks fail to compile: PyTorch >= 1.13
dropped `AT_PRIVATE_CASE_TYPE`, CUDA >= 11.5 ships a CUB that needs `CUB_NS_QUALIFIER`, and Colab's
default PyTorch needs C++17 while Cypress compiles as C++14. numpy stays at 1.23.5
because Cypress still calls `np.str`, removed in numpy 1.24. Every step stops
with a clear message if the GPU, CUDA or the CUDA kernels are missing; nothing falls back to the CPU.
First run takes about 25 minutes (CUDA toolkit download and the build).
"""),
    code("""
# 1. GPU check
import subprocess

r = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"],
                   capture_output=True, text=True, check=False)
print(r.stdout or r.stderr)
assert r.returncode == 0 and r.stdout.strip(), "No GPU. Runtime > Change runtime type > A100 GPU"
"""),
    code("""
# 2. Input boards
import os
import zipfile

from google.colab import files

if not os.path.exists("/content/cypress_in.zip"):
    uploaded = files.upload()  # pick cypress_in.zip
    name = next(iter(uploaded))
    if name != "cypress_in.zip":
        os.replace(name, "/content/cypress_in.zip")
os.makedirs("/content/in", exist_ok=True)
zipfile.ZipFile("/content/cypress_in.zip").extractall("/content/in")
print(sorted(os.listdir("/content/in")))
"""),
    code("""
%%bash
# 3. System packages, gcc 10 and the CUDA 11.3 toolkit (toolkit only; the A100 driver is Colab's)
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get -qq update
apt-get -qq install -y bison flex libboost-all-dev libcairo2-dev gcc-10 g++-10 > /dev/null
if [ ! -x /usr/local/cuda-11.3/bin/nvcc ]; then
  wget -q https://developer.download.nvidia.com/compute/cuda/11.3.1/local_installers/cuda_11.3.1_465.19.01_linux.run -O /content/cuda113.run
  sh /content/cuda113.run --silent --toolkit --toolkitpath=/usr/local/cuda-11.3 --override --no-man-page
  rm -f /content/cuda113.run
fi
/usr/local/cuda-11.3/bin/nvcc --version | tail -n 2
gcc-10 --version | head -n 1
"""),
    code("""
%%bash
# 4. Python 3.8 environment with PyTorch 1.10.0 + CUDA 11.3 (Cypress's Dockerfile stack)
set -euo pipefail
cd /content
if [ ! -x /content/bin/micromamba ]; then
  curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | tar -xj bin/micromamba
fi
export MAMBA_ROOT_PREFIX=/content/mamba
[ -x /content/env38/bin/python ] || /content/bin/micromamba create -y -q -p /content/env38 -c conda-forge python=3.8 pip
/content/env38/bin/pip install -q torch==1.10.0+cu113 -f https://download.pytorch.org/whl/cu113/torch_stable.html
/content/env38/bin/pip install -q "numpy==1.23.5" scipy shapely matplotlib cairocffi pyunpack patool pkgconfig \\
    "cmake>=3.21,<3.28" pyyaml
/content/env38/bin/python -c "import torch; assert torch.cuda.is_available(), 'PyTorch sees no CUDA'; assert torch.version.cuda == '11.3', torch.version.cuda; print(torch.__version__, torch.version.cuda, torch.cuda.get_device_name(0))"
"""),
    code("""
%%bash
# 5. Clone and build Cypress with its CUDA kernels (about 15 minutes)
set -euo pipefail
export PATH=/content/env38/bin:/usr/local/cuda-11.3/bin:$PATH
export CUDA_HOME=/usr/local/cuda-11.3 CC=gcc-10 CXX=g++-10 CUDAHOSTCXX=g++-10
cd /content
[ -d Cypress/.git ] || git clone -q --recursive https://github.com/NVlabs/Cypress.git
cd Cypress
git submodule update --init --recursive -q
echo "Cypress $(git rev-parse HEAD)"
rm -rf build install && mkdir -p build && cd build  # never reuse a cache from another stack
if ! cmake .. -DCMAKE_INSTALL_PREFIX=/content/Cypress/install -DCMAKE_BUILD_TYPE=Release \\
      -DPython_EXECUTABLE=/content/env38/bin/python -DPYTHON_EXECUTABLE=/content/env38/bin/python \\
      -DCUDA_TOOLKIT_ROOT_DIR=/usr/local/cuda-11.3 -DCUDA_HOST_COMPILER=/usr/bin/g++-10 \\
      -DCUDA_ARCH_LIST="8.0" > cmake.log 2>&1; then
  tail -n 60 cmake.log; exit 1
fi
grep -i -E "cuda|torch" cmake.log | head -n 20 || true
if ! make -j"$(nproc)" > make.log 2>&1; then
  grep -n -i -m 40 "error" make.log; exit 1
fi
make install > install.log 2>&1
n_cuda=$(find /content/Cypress/install -name "*cuda*.so" | wc -l)
echo "CUDA kernel libraries: $n_cuda"
[ "$n_cuda" -gt 0 ] || { echo "No CUDA kernels were built: Cypress would run on the CPU. Send cmake.log."; exit 1; }
"""),
    code("%%writefile /content/cypress_run.py\n" + DRIVER),
    code("""
# 6. Place every board on the GPU
import subprocess

r = subprocess.run(["/content/env38/bin/python", "/content/cypress_run.py"], capture_output=True, text=True, check=False)
print(r.stdout[-6000:])
print(r.stderr[-6000:])
"""),
    code("""
# 7. Package the results and download cypress_out.zip
import os
import zipfile

from google.colab import files

with zipfile.ZipFile("/content/cypress_out.zip", "w", zipfile.ZIP_DEFLATED) as zf:
    for name in sorted(os.listdir("/content/out")):
        path = os.path.join("/content/out", name)
        if os.path.isfile(path):
            zf.write(path, name)
    for log in ("cmake.log", "make.log"):
        p = os.path.join("/content/Cypress/build", log)
        if os.path.exists(p):
            zf.write(p, "build_" + log)
print(zipfile.ZipFile("/content/cypress_out.zip").namelist())
files.download("/content/cypress_out.zip")
"""),
]

META = {"accelerator": "GPU", "colab": {"gpuType": "A100", "provenance": []},
        "kernelspec": {"display_name": "Python 3", "name": "python3"}, "language_info": {"name": "python"}}


def write(path, cell_list):
    nb = {"cells": cell_list, "metadata": META, "nbformat": 4, "nbformat_minor": 0}
    path.write_text(json.dumps(nb, indent=1, ensure_ascii=False), encoding="utf-8")
    print("wrote", path)


write(HERE / "cypress_colab.ipynb", cells)

# Self-contained copy: the boards ride inside the notebook, so nothing has to be uploaded.
# Built only when the export exists; it lives in .runtime (generated data, not source).
EXPORT = HERE.parents[1] / ".runtime" / "cypress" / "in" / "cypress_in.zip"
if EXPORT.is_file():
    import base64

    payload = base64.b64encode(EXPORT.read_bytes()).decode("ascii")
    embedded = code(
        "# 0. Boards embedded in this notebook (no upload needed)\n"
        "import base64\n\n"
        f'DATA = "{payload}"\n'
        'with open("/content/cypress_in.zip", "wb") as fh:\n'
        "    fh.write(base64.b64decode(DATA))\n"
        'print("cypress_in.zip ready")\n')
    intro = md(cells[0]["source"] and "".join(cells[0]["source"]).replace(
        "2. **Runtime > Run all**. Cell 2 asks for `cypress_in.zip` (from `pixi run cypress export`).",
        "2. **Runtime > Run all**. The boards are already inside this notebook; nothing to upload."))
    write(EXPORT.parent / "cypress_colab_selfcontained.ipynb", [intro, embedded, *cells[1:]])
