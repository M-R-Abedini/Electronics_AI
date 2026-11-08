# SPDX-License-Identifier: MIT
"""One-time Pro Score calibration against the user's judgement (plan F2, task 4; architecture 8.3).

page  ``pixi run calibrate page`` renders every dev board's candidates
      (expert, placer_v2, random_legal, v1_constructor from the F1 bench) as
      routed copper, blind: letters in a seeded shuffle, no numbers.  The user
      ranks them per board and copies the answers back.
fit   ``pixi run calibrate fit answers.json`` searches the weights of the
      four scored components (lint keeps its reserved 10 until F6) in steps
      of 5 for the best pairwise agreement with the ranking; among equally
      good weights it keeps the one closest to the declared 35/25/15/15.

Pro Score only ranks legal candidates; it never replaces a gate.
"""

from __future__ import annotations

import argparse
import html
import itertools
import json
import random
import sys
from pathlib import Path

from ..intent import infer
from ..io.kicad import read_board
from ..render import _CSS, svg
from ..toolchain import RUNTIME_DIR
from .score import WEIGHTS, components, evaluate

OUT = RUNTIME_DIR / "f2" / "calibration"
SOURCES = {"video": "bench_gate", "royalblue54L_feather": "bench_gate"}  # 30-minute routes where they exist
BOARDS = ("stickhub", "pic_programmer", "royalblue54L_feather", "video", "ecc83")
BASELINES = ("expert", "placer_v2", "random_legal", "v1_constructor")
SCORED = ("routability", "critical_loops", "ratsnest", "regularity")
STEP = 5


def candidates(root: Path = RUNTIME_DIR / "f1") -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    for board in BOARDS:
        base = root / SOURCES.get(board, "bench_dev") / board
        for name in BASELINES:
            route = base / f"route_{name}" / "run_report.json"
            if not route.is_file():
                continue
            rr = json.loads(route.read_text(encoding="utf-8"))
            routed = read_board(rr["artifacts"]["routed_board"])
            ev = evaluate(routed, rr["metrics"], intent=infer.draft(routed))
            out.setdefault(board, {})[name] = {"board": routed, "evaluation": ev, "route": rr["metrics"],
                                               "components": components(ev["metrics"], rr["metrics"])}
    return out


def _letters(board: str, names: list[str]) -> dict[str, str]:
    order = list(names)
    random.Random(f"calibration:{board}").shuffle(order)
    return {chr(ord("A") + i): name for i, name in enumerate(order)}


_PAGE_JS = """
function collect(){const out={boards:{},comments:{}};let ok=true;
document.querySelectorAll('section[data-board]').forEach(s=>{const b=s.dataset.board;const r={};const used=new Set();
s.querySelectorAll('select').forEach(x=>{if(!x.value){ok=false;return}r[x.dataset.letter]=+x.value;
if(used.has(x.value))ok=false;used.add(x.value)});out.boards[b]=r;
const c=s.querySelector('textarea').value.trim();if(c)out.comments[b]=c});return [ok,out]}
function copyAnswers(){const [ok,out]=collect();const t=JSON.stringify(out);const box=document.getElementById('answers');
box.value=t;box.style.display='block';box.select();
document.getElementById('status').textContent=ok?'Copied. Paste it into the chat.':
'Some boards are not ranked yet, or two candidates share a rank. Copied anyway.';
try{navigator.clipboard.writeText(t)}catch(e){}}
"""


def page(cands: dict[str, dict[str, dict]], out_dir: Path = OUT) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    key, sections = {}, []
    for board, by_name in cands.items():
        letters = _letters(board, sorted(by_name))
        key[board] = letters
        n = len(letters)
        cards = []
        for letter, name in letters.items():
            c = by_name[name]
            options = "".join(f"<option value='{i}'>{i}</option>" for i in range(1, n + 1))
            cards.append(f"<div class='card'><h2>{letter}</h2>{svg(c['board'], c['evaluation'], width_px=520)}"
                         f"<p><label>Rank <select data-letter='{letter}'><option value=''>-</option>{options}</select>"
                         f"</label> <span class='hint'>1 = most professional</span></p></div>")
        sections.append(f"<section data-board='{html.escape(board)}'><h1>{html.escape(board)}</h1>"
                        f"<div class='grid'>{''.join(cards)}</div>"
                        "<p><textarea rows='2' cols='80' placeholder='Optional: why (placement, loops, "
                        "routing, neatness)'></textarea></p></section>")
    intro = ("<h1>Pro Score calibration (plan F2)</h1><p class='legend'>Each board shows the same design placed and "
             "routed in different ways (strict Freerouting, same rules and time budget). The names and the numbers "
             "are hidden on purpose. For each board, rank the placements from 1 (most professional) to the last: "
             "judge it as you would a design review (critical loops, decap and crystal placement, flow, routability, "
             "neatness). Then press the button at the bottom and paste the text into the chat.</p>"
             "<p class='legend'>Blue = front parts, red dashed = back parts, coloured lines = routed copper by layer, "
             "grey lines = connections the router could not finish.</p>")
    css = _CSS + "section{margin-bottom:28px} .hint{color:var(--muted);font-size:12px} select{font-size:14px} " \
                 "textarea{width:100%;max-width:820px;font:13px system-ui} button{font-size:15px;padding:8px 14px}"
    doc = (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' "
           f"content='width=device-width, initial-scale=1'><title>Pro Score calibration</title><style>{css}</style>"
           f"</head><body>{intro}{''.join(sections)}<p><button onclick='copyAnswers()'>Copy my answers</button> "
           f"<span id='status'></span></p><textarea id='answers' rows='4' style='display:none;width:100%'>"
           f"</textarea><script>{_PAGE_JS}</script></body></html>")
    path = out_dir / "calibration.html"
    path.write_text(doc, encoding="utf-8")
    key_path = out_dir / "calibration_key.json"
    key_path.write_text(json.dumps({"key": key, "components": {b: {n: c["components"] for n, c in by.items()}
                                                                 for b, by in cands.items()}}, indent=1),
                        encoding="utf-8")
    return path, key_path


def _score(parts: dict, weights: dict[str, int]) -> float:
    used = {k: w for k, w in weights.items() if parts.get(k) is not None and w}
    total = sum(used.values())
    return sum(parts[k] * w for k, w in used.items()) / total if total else 0.0


def agreement(pairs: list[tuple[dict, dict]], weights: dict[str, int]) -> float:
    """Fraction of user-ordered pairs (better, worse) that the weighted score orders the same way."""
    if not pairs:
        return 0.0
    return sum(_score(a, weights) > _score(b, weights) for a, b in pairs) / len(pairs)


def fit(answers: dict, key_file: Path = OUT / "calibration_key.json") -> dict:
    saved = json.loads(key_file.read_text(encoding="utf-8"))
    pairs, used_boards = [], []
    for board, ranks in answers["boards"].items():
        letters = saved["key"].get(board)
        if not letters or len(set(ranks.values())) != len(ranks):
            continue
        comps = saved["components"][board]
        ordered = sorted(ranks, key=lambda letter: ranks[letter])
        pairs += [(comps[letters[a]], comps[letters[b]]) for a, b in itertools.combinations(ordered, 2)]
        used_boards.append(board)
    prior = {k: WEIGHTS[k] for k in SCORED}
    budget = sum(prior.values())
    best = None
    for combo in itertools.product(range(0, budget + 1, STEP), repeat=len(SCORED) - 1):
        last = budget - sum(combo)
        if last < 0:
            continue
        w = dict(zip(SCORED, (*combo, last), strict=True))
        a = agreement(pairs, w)
        dist = sum(abs(w[k] - prior[k]) for k in SCORED)
        if best is None or (a, -dist) > (best[0], -best[1]):
            best = (a, dist, w)
    a, dist, w = best
    return {"boards": used_boards, "pairs": len(pairs), "prior": prior, "prior_agreement": round(agreement(pairs, prior), 4),
            "weights": w, "agreement": round(a, 4), "l1_from_prior": dist, "comments": answers.get("comments", {}),
            "lint_reserved": WEIGHTS["lint"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="calibrate", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("page")
    f = sub.add_parser("fit")
    f.add_argument("answers", help="JSON copied from the page (file path or the JSON text)")
    args = ap.parse_args(argv)
    if args.cmd == "page":
        path, key = page(candidates())
        print(json.dumps({"page": str(path), "key": str(key)}, indent=1))
        return 0
    text = Path(args.answers).read_text(encoding="utf-8") if Path(args.answers).is_file() else args.answers
    result = fit(json.loads(text))
    (OUT / "calibration_fit.json").write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
