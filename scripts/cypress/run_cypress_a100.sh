#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Cypress baseline on a CUDA machine (plan F1 task 5).  Usage:
#
#   bash run_cypress_a100.sh <boards_in_dir> <results_out_dir>
#
# <boards_in_dir> is what `pixi run cypress export` wrote (copy the folder over).
# Bring <results_out_dir> back and run `pixi run cypress import <results_out_dir>`.
#
# Needs: git, docker with the NVIDIA container toolkit (or set NO_DOCKER=1 and
# have a DREAMPlace build of Cypress in $CYPRESS_DIR).
set -euo pipefail

IN="$(cd "$1" && pwd)"
OUT="$(mkdir -p "$2" && cd "$2" && pwd)"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CYPRESS_DIR="${CYPRESS_DIR:-$HOME/Cypress}"
CYPRESS_REF="${CYPRESS_REF:-main}"

if [ ! -d "$CYPRESS_DIR/.git" ]; then
  git clone --recursive https://github.com/NVlabs/Cypress.git "$CYPRESS_DIR"
fi
git -C "$CYPRESS_DIR" fetch --quiet origin "$CYPRESS_REF" && git -C "$CYPRESS_DIR" checkout --quiet "$CYPRESS_REF"
git -C "$CYPRESS_DIR" submodule update --init --recursive
echo "Cypress at $(git -C "$CYPRESS_DIR" rev-parse HEAD)" | tee "$OUT/cypress_commit.txt"

BUILD='cd /Cypress && mkdir -p build && cd build && cmake .. -DCMAKE_INSTALL_PREFIX=/Cypress/install > /dev/null && make -j"$(nproc)" > /dev/null && make install > /dev/null'
RUN='python /driver/cypress_driver.py --in /data/in --out /data/out --cypress /Cypress'

if [ "${NO_DOCKER:-0}" = "1" ]; then
  (cd "$CYPRESS_DIR" && mkdir -p build && cd build && cmake .. -DCMAKE_INSTALL_PREFIX="$CYPRESS_DIR/install" && make -j"$(nproc)" && make install)
  python "$HERE/cypress_driver.py" --in "$IN" --out "$OUT" --cypress "$CYPRESS_DIR" 2>&1 | tee "$OUT/driver.log"
else
  docker build -t cypress-pcb "$CYPRESS_DIR" 2>&1 | tail -n 5
  docker run --rm --gpus all \
    -v "$CYPRESS_DIR":/Cypress -v "$IN":/data/in:ro -v "$OUT":/data/out -v "$HERE":/driver:ro \
    cypress-pcb bash -lc "$BUILD && $RUN" 2>&1 | tee "$OUT/driver.log"
fi
echo "done: bring $OUT back and run: pixi run cypress import <that folder>"
