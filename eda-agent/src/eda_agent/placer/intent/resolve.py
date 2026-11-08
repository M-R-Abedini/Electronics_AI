# SPDX-License-Identifier: MIT
"""The intent a run uses: the user's file if given (must validate), else the netlist draft."""

from __future__ import annotations

from pathlib import Path

from ..ir import Board
from . import infer, model, validate


class IntentError(ValueError):
    pass


def resolve(board: Board, intent_path: str | Path | None = None) -> tuple[dict, dict]:
    if intent_path is None:
        intent = infer.draft(board)
        return intent, {"source": "draft", "path": None, "sha256": model.intent_hash(intent),
                        "questions": len(intent["questions"])}
    intent = model.load(intent_path)
    rep = validate.validate(intent, board)
    if not rep["valid"]:
        raise IntentError("invalid design_intent: " + "; ".join(rep["errors"][:10]))
    return intent, {"source": "file", "path": str(Path(intent_path).resolve()), "sha256": rep["intent_sha256"],
                    "warnings": rep["warnings"], "questions": len(rep["questions"])}


def side_policy(intent: dict, requested: str | None) -> str:
    """An explicit request wins; else the intent's policy; else keep the expert's sides (P2)."""
    if requested:
        return requested
    claim = intent.get("policies", {}).get("side_policy")
    return claim["value"] if claim else "expert"
