# SPDX-License-Identifier: MIT
"""Label review page (plan F2, task 3): every dev label with its evidence and the draft's answer next to it."""

from __future__ import annotations

import html
from pathlib import Path

from .. import corpus
from ..io.kicad import read_board
from ..render import _CSS
from . import evaluate, infer, model

_EXTRA = """
table.labels { width:100%; max-width:1100px; } table.labels td, table.labels th { padding:3px 8px; text-align:left;
  border-bottom:1px solid var(--line); vertical-align:top; font-size:13px; }
tr.wrong td { background:rgba(255,120,0,.16); } tr.uncertain td { background:rgba(120,160,255,.14); }
tr.crit td:first-child { font-weight:600; } .muted { color:var(--muted); } details { margin:10px 0 22px; }
summary { cursor:pointer; font-size:16px; font-weight:600; }
"""


def write_review(out: Path) -> Path:
    sections, totals = [], []
    for entry in corpus.entries("dev"):
        board = read_board(entry.path)
        fps = {fp.ref: fp for fp in board.footprints}
        truth = evaluate.labels(entry.name)
        intent = infer.draft(board)
        res = evaluate.score(intent, truth)
        status = {r["ref"]: r for r in res["wrong"] + res["uncertain"]}
        roles = {r: p["role"] for r, p in truth["parts"].items()}
        rows = []
        for ref, lab in sorted(truth["parts"].items(), key=lambda kv: model.natural_key(kv[0])):
            fp = fps[ref]
            st = status.get(ref)
            part = intent["parts"].get(ref, {})
            tv = (part.get("target") or {}).get("value") or {}
            draft = f"{part['role']['value']}" + (f" → {tv.get('ref') or '/'.join(tv.get('candidates', []))}"
                                                  if tv.get("ref") or tv.get("candidates") else "")
            crit = model.is_critical(lab["role"], roles.get(lab.get("target")))
            cls = " ".join(filter(None, [st["status"] if st else "", "crit" if crit else ""]))
            why = " · ".join(filter(None, [lab.get("evidence"), lab.get("note")]))
            rows.append(f"<tr class='{cls}'><td>{html.escape(ref)}</td><td>{html.escape(fp.value)}</td>"
                        f"<td>{html.escape(lab['role'])}</td><td>{html.escape(lab.get('target') or '')}</td>"
                        f"<td class='muted'>{html.escape(why)}</td><td>{html.escape(draft)}</td></tr>")
        r = res["roles"]
        totals.append(f"{entry.name}: {r['correct']}/{r['decided']} decided, {r['uncertain']} uncertain")
        sections.append(
            f"<details open><summary>{html.escape(entry.name)} ({len(rows)} parts; draft disagrees on "
            f"{len(res['wrong'])}, uncertain on {len(res['uncertain'])})</summary>"
            "<table class='labels'><tr><th>Ref</th><th>Value</th><th>Label role</th><th>Label target</th>"
            "<th>Evidence / note</th><th>Engine draft</th></tr>" + "".join(rows) + "</table></details>")
    doc = (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, "
           f"initial-scale=1'><title>F2 label review</title><style>{_CSS}{_EXTRA}</style></head><body>"
           "<h1>F2: dev labels for your review</h1><p class='legend'>Each row is my label for one part. Role "
           "definitions: eda-agent/src/eda_agent/placer/intent/LABELS.md. The decap and ESD targets come from where "
           "the expert placed the part (evidence column); the engine never sees placement. Orange rows: the engine "
           "disagrees with the label. Blue rows: the engine left the role or target uncertain (a question for the "
           "model or you). Bold reference: a critical role. Tell me any label you would change, for example "
           "\"video R14 is signal_passive\".</p><p class='legend'>" + " · ".join(totals) + "</p>"
           + "".join(sections) + "</body></html>")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    return out
