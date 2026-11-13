# SPDX-License-Identifier: MIT
"""``pixi run intent draft|validate|eval`` (plan F2)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import corpus
from ..io.kicad import read_board
from ..toolchain import RUNTIME_DIR
from . import evaluate, infer, model, validate


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="intent", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("draft", help="draft design_intent.json from the netlist")
    d.add_argument("board")
    d.add_argument("--out", help="output file (never overwritten)")
    d.add_argument("--altium-project", help="read a saved .PcbDoc and this .PrjPcb directly")
    v = sub.add_parser("validate", help="check an intent against its board")
    v.add_argument("board")
    v.add_argument("intent")
    v.add_argument("--altium-project", help="read a saved .PcbDoc and this .PrjPcb directly")
    e = sub.add_parser("eval", help="draft accuracy against the dev labels (F2 gate)")
    e.add_argument("--split", default="dev")
    e.add_argument("--out", default=str(RUNTIME_DIR / "f2" / "intent_eval.json"))
    r = sub.add_parser("review", help="HTML table of the dev labels for the user's review")
    r.add_argument("--out", default=str(RUNTIME_DIR / "f2" / "labels_review.html"))
    args = ap.parse_args(argv)

    if args.cmd == "review":
        from .review import write_review

        print(write_review(Path(args.out)))
        return 0

    if args.cmd in {"draft", "validate"}:
        if args.altium_project:
            from ..io.altium import read_intent_board

            board = read_intent_board(args.board, args.altium_project)
        else:
            board = read_board(args.board)

    if args.cmd == "draft":
        intent = infer.draft(board)
        out = Path(args.out) if args.out else RUNTIME_DIR / "intent" / f"{board.name}.design_intent.json"
        if out.exists():
            print(f"refusing to overwrite {out}", file=sys.stderr)
            return 2
        out.parent.mkdir(parents=True, exist_ok=True)
        model.dump(intent, out)
        summary = {"intent": str(out), "sha256": model.intent_hash(intent), "questions": len(intent["questions"])}
        if args.altium_project:
            summary.update(source=board.source, unknown_count=len(board.unknowns), placement_geometry_complete=False)
        print(json.dumps(summary, indent=1))
        return 0
    if args.cmd == "validate":
        rep = validate.validate(model.load(args.intent), board)
        print(json.dumps(rep, indent=1, ensure_ascii=False))
        return 0 if rep["valid"] else 1

    results = {}
    for entry in corpus.entries(args.split):
        intent = infer.draft(read_board(entry.path))
        results[entry.name] = evaluate.score(intent, evaluate.labels(entry.name))
    total = evaluate.pooled(results)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"pooled": total, "boards": results}, indent=1, ensure_ascii=False), encoding="utf-8")
    table = {name: {k: r[k] for k in ("roles", "critical")} | {"pairs": r["pairs"]} for name, r in results.items()}
    print(json.dumps({"boards": table, "pooled": total, "report": str(out)}, indent=1, ensure_ascii=False))
    return 0 if all(g["passed"] for g in total["gates"]) else 1


if __name__ == "__main__":
    sys.exit(main())
