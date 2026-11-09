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
import base64
import hashlib
import html
import itertools
import json
import random
import shutil
import subprocess
import sys
from pathlib import Path

from .. import corpus
from ..intent import infer
from ..io.kicad import read_board
from ..render import _CSS, svg
from ..sexpr import parse
from ..toolchain import RUNTIME_DIR, kicad, sha256_file
from .score import WEIGHTS, components, evaluate

OUT = RUNTIME_DIR / "f2" / "calibration"
SOURCES = {"video": "bench_gate", "royalblue54L_feather": "bench_gate"}  # 30-minute routes where they exist
BOARDS = ("stickhub", "pic_programmer", "royalblue54L_feather", "video", "ecc83")
BASELINES = ("expert", "placer_v2", "random_legal", "v1_constructor")
SCORED = ("routability", "critical_loops", "ratsnest", "regularity")
STEP = 5
ADOPT_GAIN = 0.05  # declared: minimum agreement gain over the prior weights


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
                                               "components": components(ev["metrics"], rr["metrics"]),
                                               "file": Path(rr["artifacts"]["routed_board"]),
                                               "project": Path(corpus.get(board).path).parent}
    return out


def _blind_copy(src: Path, dst: Path, project: Path) -> Path:
    """The board without board-level text and silkscreen drawings: titles and labels sit where the expert put
    them and would give the expert's candidate away (seen on pic_programmer: "PIC 18 PINS" on empty board)."""
    text = src.read_text(encoding="utf-8")
    root = parse(text)
    cut = [n for n in root.children() if n.tag in ("gr_text", "gr_text_box", "dimension")
           or ((n.tag or "").startswith("gr_") and str(n.value("layer")) in ("F.SilkS", "B.SilkS"))]
    for n in sorted(cut, key=lambda n: -n.start):
        text = text[:n.start] + text[n.end:]
    # project-local 3D models (${KIPRJMOD}/3dmodels/...) live next to the original board, not the routed copy
    text = text.replace("${KIPRJMOD}", project.resolve().as_posix())
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text, encoding="utf-8")
    pro = src.with_suffix(".kicad_pro")
    if pro.is_file():
        shutil.copyfile(pro, dst.with_suffix(".kicad_pro"))
    return dst


RENDER_LONG_PX = 1300


def renders(src: Path, board, project: Path, cache: Path = OUT / "renders") -> dict[str, Path]:
    """KiCad 3D renders with the real part models (top, and bottom when parts sit there), cached by file hash.

    The frame follows the board's aspect ratio so a tall board (StickHub) is not cropped."""
    work = cache / f"{sha256_file(src)[:16]}-v2"
    x0, y0, x1, y1 = board.outline_box or (0.0, 0.0, 1.0, 1.0)
    aspect = max(x1 - x0, 1e-3) / max(y1 - y0, 1e-3)
    if aspect >= 1:
        width, height = RENDER_LONG_PX, max(420, round(RENDER_LONG_PX / aspect * 1.12))
    else:
        width, height = max(420, round(RENDER_LONG_PX * aspect * 1.12)), RENDER_LONG_PX
    sides = ["top"] + (["bottom"] if any(fp.side == "B" for fp in board.footprints) else [])
    out = {}
    for side in sides:
        target = work / f"{side}.jpg"
        if not target.is_file():
            copy = _blind_copy(src, work / "board" / "blind.kicad_pcb", project)
            subprocess.run([str(kicad().cli), "pcb", "render", "--side", side, "--width", str(width),
                            "--height", str(height), "--quality", "high", "--background", "opaque", "-o", str(target),
                            str(copy)], stdin=subprocess.DEVNULL, capture_output=True, timeout=600, check=True)
        out[side] = target
    return out


def _img(path: Path, alt: str) -> str:
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"<img class='r3d' alt='{html.escape(alt)}' src='data:image/jpeg;base64,{data}'>"


VIEW_NAMES = {"top": "3D top", "bottom": "3D bottom", "routing": "Routing"}


def _letters(board: str, names: list[str]) -> dict[str, str]:
    order = list(names)
    random.Random(f"calibration:{board}").shuffle(order)
    return {chr(ord("A") + i): name for i, name in enumerate(order)}


_PAGE_CSS = (
    "section{margin-bottom:28px} .hint{color:var(--muted);font-size:12px} select{font-size:14px} "
    "textarea{width:100%;max-width:820px;font:13px system-ui} button{font-size:15px;padding:8px 14px} "
    ".card{width:540px} .tabs button{font-size:13px;padding:4px 10px;margin:0 4px 6px 0;border-radius:6px;"
    "border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer} "
    ".tabs button.on{background:#2f6fde;color:#fff;border-color:#2f6fde} "
    "img.r3d{width:100%;border-radius:4px;cursor:zoom-in} "
    "#zoom{position:fixed;inset:0;background:rgba(0,0,0,.88);display:none;align-items:center;"
    "justify-content:center;z-index:9;cursor:zoom-out} #zoom img{max-width:96vw;max-height:94vh} "
    "@media(max-width:600px){.card{width:100%}}")

_PAGE_JS = """
function view(b,i){const c=b.closest('.card');
c.querySelectorAll('.tabs button').forEach((x,j)=>x.classList.toggle('on',j===i));
c.querySelectorAll('.pane').forEach((p,j)=>p.hidden=j!==i)}
document.addEventListener('click',e=>{if(e.target.matches('img.r3d')){const z=document.getElementById('zoom');
z.querySelector('img').src=e.target.src;z.style.display='flex'}});
const KEY='calibration:'+document.body.dataset.version;
function save(){try{localStorage.setItem(KEY,JSON.stringify(collect()[1]))}catch(e){}}
function restore(){let d=null;try{d=JSON.parse(localStorage.getItem(KEY)||'null')}catch(e){}if(!d)return;
document.getElementById('rater').value=d.rater||'';
document.querySelectorAll('section[data-board]').forEach(s=>{const b=s.dataset.board;const r=(d.boards||{})[b]||{};
s.querySelectorAll('select').forEach(x=>{if(r[x.dataset.letter])x.value=String(r[x.dataset.letter])});
s.querySelector('textarea').value=(d.comments||{})[b]||''})}
document.addEventListener('change',save);document.addEventListener('input',save);restore();
function collect(){const out={version:document.body.dataset.version,rater:document.getElementById('rater').value.trim(),
boards:{},comments:{}};let ok=true;
document.querySelectorAll('section[data-board]').forEach(s=>{const b=s.dataset.board;const r={};const used=new Set();
s.querySelectorAll('select').forEach(x=>{if(!x.value){ok=false;return}r[x.dataset.letter]=+x.value;
if(used.has(x.value))ok=false;used.add(x.value)});out.boards[b]=r;
const c=s.querySelector('textarea').value.trim();if(c)out.comments[b]=c});return [ok,out]}
function copyAnswers(){const [ok,out]=collect();const t=JSON.stringify(out);const box=document.getElementById('answers');
box.value=t;box.style.display='block';box.select();
if(!out.rater)ok=false;
document.getElementById('status').textContent=ok?'Copied. Send this text back (chat, email or messenger).':
'Your name is missing, a board is not ranked yet, or two candidates share a rank. Copied anyway.';
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
            views = [(v, _img(path, f"{letter} {v}")) for v, path in renders(c["file"], c["board"], c["project"]).items()]
            views.append(("routing", svg(c["board"], c["evaluation"], width_px=520)))
            tabs = "".join(f"<button type='button' onclick='view(this,{i})'{' class=on' if i == 0 else ''}>"
                           f"{VIEW_NAMES[v]}</button>" for i, (v, _h) in enumerate(views))
            panes = "".join(f"<div class='pane'{'' if i == 0 else ' hidden'}>{h}</div>"
                            for i, (_v, h) in enumerate(views))
            cards.append(f"<div class='card'><h2>{letter}</h2><div class='tabs'>{tabs}</div>{panes}"
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
             "neatness). Then press the button at the bottom and send the text back. Your choices stay in this "
             "browser if you close the page, but nothing is sent anywhere by itself.</p>"
             "<p><label>Your name <input id='rater' size='28'></label></p>"
             "<p class='legend'>Each candidate has views: <b>3D top</b> and <b>3D bottom</b> are KiCad renders with "
             "the real part models (the bottom view is seen from below, so it is mirrored); <b>Routing</b> is a flat "
             "drawing: blue = front parts, red dashed = back parts, coloured lines = routed copper by layer, grey lines "
             "= connections the router could not finish. Board titles and labels are removed from every candidate "
             "alike. Click an image to enlarge it.</p>")
    css = _CSS + _PAGE_CSS
    version = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:12]
    doc = (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' "
           f"content='width=device-width, initial-scale=1'><title>Pro Score calibration</title><style>{css}</style>"
           f"</head><body data-version='{version}'>{intro}{''.join(sections)}<p><button onclick='copyAnswers()'>"
           f"Copy my answers</button> <span id='status'></span></p><textarea id='answers' rows='4' "
           f"style='display:none;width:100%'></textarea><div id='zoom' onclick=\"this.style.display='none'\">"
           f"<img alt=''></div><script>{_PAGE_JS}</script></body></html>")
    path = out_dir / "calibration.html"
    path.write_text(doc, encoding="utf-8")
    key_path = out_dir / "calibration_key.json"
    key_path.write_text(json.dumps({"version": version, "key": key, "components": {b: {n: c["components"] for n, c in by.items()}
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


def _pairs(answers: dict, saved: dict) -> tuple[list[tuple[dict, dict]], list[str]]:
    """Every preference the rater actually stated, from complete or partial rankings.

    Two ranked candidates with different ranks give one pair; a tie gives none.
    A candidate ranked 1 is the rater's best, so it also beats every unranked
    one.  Nothing else is inferred about unranked candidates."""
    pairs, used = [], []
    for board, ranks in answers["boards"].items():
        letters = saved["key"].get(board)
        if not letters:
            continue
        ranks = {k: int(v) for k, v in ranks.items() if k in letters}
        comps = saved["components"][board]
        found = [(a, b) for a, b in itertools.permutations(ranks, 2) if ranks[a] < ranks[b]]
        found += [(a, u) for a in ranks if ranks[a] == 1 for u in letters if u not in ranks]
        if found:
            pairs += [(comps[letters[a]], comps[letters[b]]) for a, b in found]
            used.append(board)
    return pairs, used


def fit(all_answers: list[dict], key_file: Path = OUT / "calibration_key.json") -> dict:
    """Pairs from every rater are pooled, so each rater counts once per board they ranked."""
    saved = json.loads(key_file.read_text(encoding="utf-8"))
    pairs, raters = [], []
    for answers in all_answers:
        if answers.get("version") not in (None, saved.get("version")):
            raise ValueError(f"answers of {answers.get('rater')!r} are for another calibration page")
        p, used = _pairs(answers, saved)
        pairs += p
        raters.append({"rater": answers.get("rater") or "?", "boards": used, "pairs": len(p)})
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
    base = agreement(pairs, prior)
    adopt = a - base >= ADOPT_GAIN  # a gain of one or two pairs is noise, not evidence (P4)
    return {"raters": raters, "pairs": len(pairs), "prior": prior,
            "prior_agreement": round(base, 4), "best_weights": w, "best_agreement": round(a, 4),
            "l1_from_prior": dist, "adopt": adopt, "weights": w if adopt else prior,
            "rule": f"adopt the fitted weights only if they agree with {ADOPT_GAIN:.0%} more pairs than the prior", "comments": {a.get("rater") or "?": a.get("comments", {}) for a in all_answers},
            "lint_reserved": WEIGHTS["lint"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="calibrate", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("page")
    f = sub.add_parser("fit")
    f.add_argument("answers", nargs="+", help="answers copied from the page: one file or JSON text per rater")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")  # rater names are any script; the Windows console default is cp1252
    if args.cmd == "page":
        path, key = page(candidates())
        print(json.dumps({"page": str(path), "key": str(key)}, indent=1))
        return 0
    texts = [Path(a).read_text(encoding="utf-8") if Path(a).is_file() else a for a in args.answers]
    result = fit([json.loads(t) for t in texts])
    (OUT / "calibration_fit.json").write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
