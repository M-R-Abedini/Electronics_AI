# SPDX-License-Identifier: MIT
"""Accuracy of a drafted intent against the dev labels (plan F2 acceptance).

Gate: role accuracy >= 90 % and critical-role accuracy >= 95 % on dev, with
the uncertain claims reported separately.  A part counts as correct when its
role is right and, for a role with a labelled target, the target is right.
A part is uncertain when the draft's role claim is uncertain, or when the
draft gives the target only as candidates or with low confidence.  "strict"
counts every uncertain part as wrong, so the separate report cannot hide a
weak draft.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import model

GOLDEN = Path(__file__).resolve().parents[4] / "tests" / "placer" / "golden" / "intent"
ROLE_GATE, CRITICAL_GATE = 0.90, 0.95


def labels(name: str, golden: Path = GOLDEN) -> dict:
    return json.loads((golden / f"{name}.labels.json").read_text(encoding="utf-8"))


def _rate(correct: int, n: int, uncertain: int) -> dict:
    decided = n - uncertain
    return {"n": n, "uncertain": uncertain, "decided": decided, "correct": correct,
            "accuracy": round(correct / decided, 4) if decided else None,
            "strict": round(correct / n, 4) if n else None,
            "coverage": round(decided / n, 4) if n else None}


def score(intent: dict, truth: dict) -> dict:
    roles = {r: p["role"] for r, p in truth["parts"].items()}
    rows = []
    for ref, lab in truth["parts"].items():
        part = intent["parts"].get(ref)
        lrole, ltarget = lab["role"], lab.get("target")
        critical = model.is_critical(lrole, roles.get(ltarget) if ltarget else None)
        row = {"ref": ref, "label": lrole, "label_target": ltarget, "critical": critical}
        if part is None or model.is_uncertain(part["role"]) or part["role"]["value"] == "unknown":
            row.update(status="uncertain", about="role", draft=part["role"]["value"] if part else None)
            rows.append(row)
            continue
        drole = part["role"]["value"]
        row["draft"] = drole
        if drole != lrole:
            row.update(status="wrong", about="role")
            rows.append(row)
            continue
        if ltarget:
            tgt = part.get("target")
            tv = tgt["value"] if tgt else {}
            row["draft_target"] = tv.get("ref") or tv.get("candidates")
            if not tgt or tv.get("ref") is None or model.is_uncertain(tgt):
                row.update(status="uncertain", about="target",
                           label_in_candidates=ltarget in (tv.get("candidates") or [tv.get("ref")]))
            elif tv["ref"] != ltarget:
                row.update(status="wrong", about="target")
            else:
                row["status"] = "correct"
        else:
            row["status"] = "correct"
        rows.append(row)

    def summary(sel: list[dict]) -> dict:
        return _rate(sum(r["status"] == "correct" for r in sel), len(sel), sum(r["status"] == "uncertain" for r in sel))

    crit = [r for r in rows if r["critical"]]
    truth_pairs = {tuple(p) for p in truth.get("pairs", [])}
    claimed = {(p["value"]["positive"], p["value"]["negative"]): p for p in intent.get("diff_pairs", [])}
    confident = {k for k, p in claimed.items() if not model.is_uncertain(p)}
    pairs = {"labelled": len(truth_pairs), "found": len(confident & truth_pairs),
             "false": sorted(map(list, confident - truth_pairs)),
             "missed": sorted(map(list, truth_pairs - set(claimed))),
             "uncertain": sorted(map(list, (set(claimed) - confident) & truth_pairs))}
    return {"roles": summary(rows), "critical": summary(crit), "pairs": pairs,
            "wrong": [r for r in rows if r["status"] == "wrong"],
            "uncertain": [r for r in rows if r["status"] == "uncertain"]}


def pooled(results: dict[str, dict]) -> dict:
    def add(key: str) -> dict:
        n = sum(r[key]["n"] for r in results.values())
        unc = sum(r[key]["uncertain"] for r in results.values())
        return _rate(sum(r[key]["correct"] for r in results.values()), n, unc)

    roles, crit = add("roles"), add("critical")
    pairs = {k: sum(r["pairs"][k] if isinstance(r["pairs"][k], int) else len(r["pairs"][k]) for r in results.values())
             for k in ("labelled", "found", "false", "missed", "uncertain")}
    gates = [
        {"name": "role_accuracy", "value": roles["accuracy"], "op": ">=", "threshold": ROLE_GATE,
         "passed": roles["accuracy"] is not None and roles["accuracy"] >= ROLE_GATE},
        {"name": "critical_role_accuracy", "value": crit["accuracy"], "op": ">=", "threshold": CRITICAL_GATE,
         "passed": crit["accuracy"] is not None and crit["accuracy"] >= CRITICAL_GATE},
        {"name": "diff_pairs_exact", "value": pairs["found"], "op": "==", "threshold": pairs["labelled"],
         "passed": pairs["found"] == pairs["labelled"] and pairs["false"] == 0},
    ]
    return {"roles": roles, "critical": crit, "pairs": pairs, "gates": gates}
