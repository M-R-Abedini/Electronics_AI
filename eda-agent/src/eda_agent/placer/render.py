# SPDX-License-Identifier: MIT
"""Review gallery: side-by-side SVG of placements with ratsnest, routing and findings (plan F0b, task 4).

One self-contained HTML file per gallery, no external assets, so it opens
from ``.runtime`` in any browser.  Front-side parts are drawn blue, back-side
parts red; legality findings are outlined in orange; pin-pair ratsnest of
signal nets is grey; routed copper (when the board carries it) is drawn per
layer underneath.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from pathlib import Path

from .eval import metrics
from .ir import Board

_LAYER_COLOURS = {"F.Cu": "#c83434", "B.Cu": "#3434c8"}
_INNER = ("#c2c200", "#c200c2", "#00c2c2", "#c27000", "#00c270", "#7000c2")


@dataclass
class Panel:
    title: str
    board: Board
    evaluation: dict | None = None
    route: dict | None = None  # route-check metrics for this placement


def _layer_colour(name: str) -> str:
    if name in _LAYER_COLOURS:
        return _LAYER_COLOURS[name]
    try:
        return _INNER[(int(name[2:].split(".")[0]) - 1) % len(_INNER)]
    except ValueError:
        return "#888"


def _pts(poly) -> str:
    return " ".join(f"{x:.3f},{y:.3f}" for x, y in poly)


def svg(board: Board, evaluation: dict | None = None, *, width_px: int = 640) -> str:
    box = board.outline_box or (0.0, 0.0, 10.0, 10.0)
    for fp in board.footprints:
        b = fp.box()
        box = (min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3]))
    pad = 2.0
    x0, y0, w, h = box[0] - pad, box[1] - pad, box[2] - box[0] + 2 * pad, box[3] - box[1] + 2 * pad
    flagged, waived = set(), set()
    if evaluation:
        for f in evaluation["legality"]["findings"]:
            if not f["fixed"]:
                (waived if f["waived_by"] else flagged).update(f["refs"])
    out = [(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0:.3f} {y0:.3f} {w:.3f} {h:.3f}" '
           f'width="{width_px}" height="{width_px * h / w:.0f}" class="board">')]
    for o in board.outline:
        out.append(f'<polygon points="{_pts(o.outer)}" class="outline"/>')
        out += [f'<polygon points="{_pts(hole)}" class="hole"/>' for hole in o.holes]
    for t in sorted(board.routing.get("tracks", []), key=lambda t: t["layer"] != "B.Cu"):
        out.append(f'<polyline points="{_pts(t["points"])}" stroke="{_layer_colour(t["layer"])}" '
                   f'stroke-width="{t["width"]}" class="track"/>')
    out += [f'<circle cx="{v[0]}" cy="{v[1]}" r="{v[2] / 2}" class="via"/>' for v in board.routing.get("vias", [])]
    for side in ("B", "F"):
        for fp in board.footprints:
            if fp.side != side:
                continue
            cls = "fp-f" if side == "F" else "fp-b"
            for poly in fp.courtyard_world():
                extra = " flagged" if fp.ref in flagged else " waived" if fp.ref in waived else ""
                out.append(f'<polygon points="{_pts(poly)}" class="{cls}{extra}"><title>{html.escape(fp.ref)} '
                           f'{html.escape(fp.value)} ({side})</title></polygon>')
            for p, (px, py) in fp.pads_world():
                r = max(min(p.size) / 2, 0.08)
                out.append(f'<circle cx="{px:.3f}" cy="{py:.3f}" r="{r:.3f}" class="pad-{side.lower()}"/>')
    planes = metrics.zone_nets(board)
    for net, pts in metrics.net_points(board).items():
        if net in planes:
            continue
        for a, b in metrics.mst_edges(pts):
            out.append(f'<line x1="{a[0]:.3f}" y1="{a[1]:.3f}" x2="{b[0]:.3f}" y2="{b[1]:.3f}" class="rat"/>')
    out.append("</svg>")
    return "".join(out)


_CSS = """
:root { color-scheme: light dark; --bg:#fafafa; --fg:#1a1a1a; --card:#fff; --line:#ddd; --muted:#666; }
@media (prefers-color-scheme: dark) { :root { --bg:#16181b; --fg:#e6e6e6; --card:#202327; --line:#353a40; --muted:#9aa; } }
body { background:var(--bg); color:var(--fg); font:14px/1.45 system-ui, sans-serif; margin:16px; }
h1 { font-size:18px; margin:0 0 12px; }
.grid { display:flex; flex-wrap:wrap; gap:16px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px; max-width:100%; }
.card h2 { font-size:15px; margin:0 0 8px; }
svg.board { max-width:100%; height:auto; background:#0d2b16; border-radius:4px; }
.outline { fill:#173d22; stroke:#d0d070; stroke-width:0.15; }
.hole { fill:#0d2b16; stroke:#d0d070; stroke-width:0.1; }
.track { fill:none; stroke-linecap:round; opacity:0.55; }
.via { fill:#bbb; opacity:0.7; }
.fp-f { fill:rgba(80,140,255,0.28); stroke:#6fa0ff; stroke-width:0.08; }
.fp-b { fill:rgba(255,90,90,0.22); stroke:#ff7a7a; stroke-width:0.08; stroke-dasharray:0.3 0.2; }
.flagged { stroke:#ff9d00; stroke-width:0.18; }
.waived { stroke:#ff9d00; stroke-width:0.06; stroke-dasharray:0.25 0.2; }
.pad-f { fill:#e8c35a; } .pad-b { fill:#c99a5a; opacity:0.75; }
.rat { stroke:#cfcfcf; stroke-width:0.06; opacity:0.6; }
table { border-collapse:collapse; margin-top:8px; font-size:13px; }
td { padding:2px 8px 2px 0; border-bottom:1px solid var(--line); vertical-align:top; }
td:first-child { color:var(--muted); }
.legend { color:var(--muted); font-size:12px; margin-bottom:12px; }
"""


def _rows(panel: Panel) -> list[tuple[str, str]]:
    b, e = panel.board, panel.evaluation
    rows = [("parts F / B", f"{b.side_counts()['F']} / {b.side_counts()['B']}"),
            ("copper layers", str(b.layer_count)),
            ("layer roles", ", ".join(f"{r.layer}:{r.role}{'(' + r.net + ')' if r.net else ''}"
                                      for r in b.stackup.roles))]
    if e:
        legal = e["legality"]
        m = e["metrics"]
        ps = e["pro_score"]
        rows += [
            ("legality", (f"{legal['violations']} violations ({legal['by_kind']}), {legal['waived']} waived, "
                         f"{legal['fixed_only']} fixed-only")),
            ("HPWL signal", f"{m['hpwl']['signal_mm']:.1f} mm"),
            ("ratsnest crossings", f"{m['crossings']['count']} / {m['crossings']['edges']} edges"),
            ("RUDY peak / over", f"{m['rudy'].get('peak')} / {m['rudy'].get('over_capacity_fraction')}"),
            ("decap pin distance", json.dumps(m["critical_loops"]["decap_pin_distance"])),
            ("Pro Score v0 (uncalibrated)", f"{ps['score']}  {json.dumps(ps['components'])}"),
        ]
    if panel.route:
        r = panel.route
        rows.append(("route check", (f"coverage {r['coverage']:.1%}, unrouted {r['unrouted']}, vias {r['vias']}, "
                                    f"{r['track_length_mm']:.0f} mm")))
    return rows


def gallery(panels: list[Panel], out: str | Path, title: str = "Placement gallery") -> Path:
    out = Path(out)
    cards = []
    for panel in panels:
        table = "".join(f"<tr><td>{html.escape(k)}</td><td>{html.escape(v)}</td></tr>" for k, v in _rows(panel))
        cards.append(f'<div class="card"><h2>{html.escape(panel.title)}</h2>{svg(panel.board, panel.evaluation)}'
                     f"<table>{table}</table></div>")
    page = (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width, initial-scale=1'><title>{html.escape(title)}</title>"
            f"<style>{_CSS}</style></head><body><h1>{html.escape(title)}</h1>"
            "<div class='legend'>blue = front parts, red dashed = back parts, orange = legality finding "
            "(thin dashed = accepted by a project rule on the input board), "
            "grey lines = pin-pair ratsnest of signal nets, coloured lines = existing copper by layer</div>"
            f"<div class='grid'>{''.join(cards)}</div></body></html>")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out
