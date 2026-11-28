#!/usr/bin/env python3
"""Graphical interaction UI for circuit-design-skill (MVP).

Builds intake/defaults-confirm and artifact-review sessions using the shared
design-interact UI. Dependency-free; stdlib only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SHARED_SCRIPTS = SKILL_DIR.parent / "_shared" / "design-interact" / "scripts"
def _load_shared_dependency():
    """Load the optional shared UI and explain how to install it when absent."""
    if not SHARED_SCRIPTS.is_dir():
        raise RuntimeError(
            "shared design-interact UI not found. Expected "
            f"'{SHARED_SCRIPTS}'. Clone/install the shared design-interact "
            "repository at that sibling path; see README.md, 'Graphical interaction'."
        )

    shared_path = str(SHARED_SCRIPTS)
    if shared_path not in sys.path:
        sys.path.insert(0, shared_path)
    try:
        import design_interact
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "could not import the shared design-interact UI "
            f"from '{SHARED_SCRIPTS}' (missing module: {exc.name}). "
            "Check the shared repository installation and its dependencies."
        ) from exc
    return design_interact


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _spec_name(spec: dict, spec_path: Path) -> str:
    return str(spec.get("name") or spec_path.stem)


def build_intake_payload(*, title: str | None = None) -> dict:
    return {
        "title": title or "Circuit design intake",
        "intro": "Answer the minimum intake questions and confirm defaults/assumptions. The agent will not silently lock revision, power, bypass, form factor, anchors, or substitutions.",
        "skill": "circuit-design-skill",
        "session_kind": "intake",
        "submit_label": "Send intake",
        "blocks": [
            {"type": "preview", "title": "Defaults confirmation gate", "format": "markdown", "content":
             "Before generating artifacts, the agent must restate locked defaults and assumptions and get explicit confirmation (`defaults confirmed`) or a list of changes. "
             "For clone/variant work, confirm target revision/topology, primary anchor source, known source conflicts, and allowed substitutions."},
            {"type": "question", "id": "function_io", "question_type": "text", "required": True,
             "prompt": "What must the circuit do, and what are the input and output?",
             "placeholder": "One sentence + input source/amplitude/impedance + output/load"},
            {"type": "question", "id": "supplies", "question_type": "text", "required": True,
             "prompt": "What supply voltage(s) are available (source, tolerance/transients if known)?",
             "placeholder": "e.g., 9 V battery, ±15 V bench, USB 5 V"},
            {"type": "question", "id": "load", "question_type": "text", "required": True,
             "prompt": "What load must it drive?",
             "placeholder": "Resistance/capacitance/current/inductive, impedance range"},
            {"type": "question", "id": "performance", "question_type": "text", "required": True,
             "prompt": "What accuracy, bandwidth, current, noise/distortion, or other performance is required?",
             "placeholder": "Targets with conditions where possible"},
            {"type": "question", "id": "deliverables", "question_type": "multi", "required": True,
             "prompt": "Which deliverables do you want?",
             "options": [
                 {"id": "spice", "label": "SPICE netlist (.cir)"},
                 {"id": "ltspice_asc", "label": "LTspice schematic (.asc)"},
                 {"id": "kicad_sch", "label": "KiCad schematic (.kicad_sch)"},
                 {"id": "kicad_net", "label": "KiCad netlist (.net)"},
                 {"id": "bom", "label": "BOM (CSV)"},
                 {"id": "report", "label": "Design report (md/json)"},
                 {"id": "pcb", "label": "PCB handoff artifacts"},
             ]},
            {"type": "question", "id": "audio_targets", "question_type": "text", "required": False,
             "prompt": "If this is audio/sound equipment: what conditions/targets come from audio-engineering-skill (or leave blank if N/A)?",
             "placeholder": "Level, frequency, load, bandwidth, weighting, measurement method"},
            {"type": "question", "id": "clone_context", "question_type": "text", "required": False,
             "prompt": "If clone/variant: target revision/topology, primary anchor source, known conflicts, allowed substitutions (or N/A)."},
            {"type": "question", "id": "defaults_assumptions", "question_type": "text", "required": True,
             "prompt": "Defaults/assumptions to lock now (agent should restate; edit here if needed). Reply with explicit confirmation text.",
             "placeholder": "defaults confirmed — OR list changes. Include any proposed defaults."},
            {"type": "review", "id": "intake_review", "prompt": "Confirm the intake is sufficient for the agent to proceed.", "required": True,
             "decisions": [
                 {"id": "proceed", "label": "Proceed with these answers/defaults", "description": "Agent may create/update spec and state assumptions."},
                 {"id": "revise", "label": "Revise answers", "description": "I need to clarify or change inputs/defaults."},
                 {"id": "stop", "label": "Stop", "description": "Do not proceed."},
             ]},
        ],
    }


def build_artifact_review_payload(*, spec_path: Path, out_dir: Path, title: str | None = None) -> dict:
    spec = _load_json(spec_path)
    name = _spec_name(spec, spec_path)
    blocks: list[dict] = []

    blocks.append({"type": "preview", "title": "Design spec summary", "format": "keyvalue", "content": {
        "name": spec.get("name"),
        "description": spec.get("description"),
        "topology": spec.get("topology"),
        "ground": spec.get("ground"),
        "components": len(spec.get("components") or []),
        "deliverables": ", ".join(spec.get("deliverables") or []),
        "analyses": ", ".join(a.get("type","?") for a in (spec.get("analyses") or []) if isinstance(a, dict)),
        "assumptions": len(spec.get("assumptions") or []),
    }})

    design = spec.get("design")
    if isinstance(design, dict):
        blocks.append({"type": "preview", "title": "Design record", "format": "json", "content": design,
                       "description": "Requirements, architecture, decisions, risks, sources."})

    metrics = spec.get("metrics")
    if metrics:
        blocks.append({"type": "preview", "title": "Metrics / conditions", "format": "json", "content": metrics})

    candidates = [
        (out_dir / f"{name}-report.md", "Design report (Markdown)", "markdown"),
        (out_dir / f"{name}-report.json", "Design report (JSON)", "json"),
        (out_dir / f"{name}-bom.csv", "Bill of materials", "table"),
        (out_dir / f"{name}.cir", "SPICE netlist", "text"),
        (out_dir / f"{name}.asc", "LTspice schematic (text)", "text"),
        (out_dir / f"{name}.net", "KiCad netlist", "text"),
        (out_dir / f"{name}-pcb-handoff.md", "PCB handoff", "markdown"),
        (out_dir / f"{name}-pinmap.json", "PCB pin map", "json"),
    ]
    present_files = []
    for p, label, fmt in candidates:
        if p.is_file():
            present_files.append(str(p.relative_to(out_dir) if out_dir in p.parents or p.parent == out_dir else p))
            blocks.append({"type": "preview", "title": label, "format": fmt, "path": str(p), "description": f"Generated artifact: {p.name}"})
    if present_files:
        blocks.append({"type": "preview", "title": "Available artifacts", "format": "files", "paths": present_files,
                       "description": "Downloads are restricted to the selected output directory."})
    else:
        blocks.append({"type": "preview", "title": "No generated artifacts found", "format": "text",
                       "content": f"Looked in: {out_dir}\nExpected files based on spec name '{name}'. Generate first, or pass the correct --out-dir."})

    blocks.append({"type": "review", "id": "artifact_review", "required": True,
                   "prompt": f"Review circuit artifacts for '{name}'.",
                   "help": "Approve to continue, request changes with specifics, or block if the design direction/assumptions are wrong.",
                   "comment_label": "Feedback / requested changes"})
    return {
        "title": title or f"Circuit artifact review: {name}",
        "intro": "MVP preview: rendered report/BOM/JSON and text artifacts with downloads. Schematic graphics are not rendered; open .asc/.kicad_sch in their tools if needed.",
        "skill": "circuit-design-skill",
        "session_kind": "artifact-review",
        "artifacts_dir": str(out_dir.resolve()),
        "submit_label": "Send review",
        "blocks": blocks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Graphical interaction UI for circuit-design-skill.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_intake = sub.add_parser("intake", help="ask intake/defaults questions")
    p_intake.add_argument("--out", required=True)
    p_intake.add_argument("--title")
    p_intake.add_argument("--host", default="127.0.0.1")
    p_intake.add_argument("--mobile", action="store_true")
    p_intake.add_argument("--port", type=int, default=8771)
    p_intake.add_argument("--timeout", type=float, default=1800)
    p_intake.add_argument("--detach", action="store_true")
    p_intake.add_argument("--open", action="store_true", dest="open_browser")
    p_intake.add_argument("--json", action="store_true", dest="print_json")

    p_rev = sub.add_parser("review-artifacts", help="review generated artifacts")
    p_rev.add_argument("--spec", required=True)
    p_rev.add_argument("--out-dir", required=True)
    p_rev.add_argument("--out", required=True, help="answer JSON path")
    p_rev.add_argument("--title")
    p_rev.add_argument("--host", default="127.0.0.1")
    p_rev.add_argument("--mobile", action="store_true")
    p_rev.add_argument("--port", type=int, default=8771)
    p_rev.add_argument("--timeout", type=float, default=1800)
    p_rev.add_argument("--detach", action="store_true")
    p_rev.add_argument("--open", action="store_true", dest="open_browser")
    p_rev.add_argument("--json", action="store_true", dest="print_json")

    args = parser.parse_args(argv)
    out_path = Path(args.out).expanduser().resolve()
    url_file = out_path.with_suffix(".url")
    status_file = out_path.with_suffix(".status.json")
    log_file = out_path.with_suffix(".log")
    host = "0.0.0.0" if args.mobile else args.host

    try:
        di = _load_shared_dependency()
    except RuntimeError as exc:
        print(f"graphical interaction unavailable: {exc}", file=sys.stderr)
        return 2

    if args.command == "intake":
        payload = di.normalize_payload(build_intake_payload(title=args.title))
        artifacts_dir = None
    else:
        spec_path = Path(args.spec).expanduser().resolve()
        out_dir = Path(args.out_dir).expanduser().resolve()
        payload = di.normalize_payload(build_artifact_review_payload(spec_path=spec_path, out_dir=out_dir, title=args.title), artifacts_dir=out_dir)
        artifacts_dir = out_dir

    if args.detach:
        return di._detach(payload=payload, out_path=out_path, host=host, port=args.port, timeout=args.timeout,
                          url_file=url_file, status_file=status_file, log_file=log_file,
                          artifacts_dir=str(artifacts_dir) if artifacts_dir else None,
                          open_browser=args.open_browser, verbose=False)
    try:
        server = di.make_server(payload, out_path=out_path, host=host, port=args.port, status_path=status_file,
                                artifacts_dir=artifacts_dir)
    except OSError as exc:
        print(f"could not start interaction server: {exc}", file=sys.stderr)
        return 1
    return di.run_server(server, timeout=args.timeout, url_file=url_file, open_browser=args.open_browser, print_json=args.print_json)


if __name__ == "__main__":
    raise SystemExit(main())
