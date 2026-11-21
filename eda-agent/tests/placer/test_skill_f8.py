# SPDX-License-Identifier: MIT
"""F8 verification: Pro PCB Designer skill contract parity and cross-client determinism (P7)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from eda_agent.placer import silkscreen
from eda_agent.placer.eval.score import evaluate
from eda_agent.placer.intent import infer, model, validate
from eda_agent.placer.ir import (
    Board,
    CopperLayer,
    Courtyard,
    Footprint,
    Net,
    NetClass,
    Outline,
    Pad,
    derive,
)
from eda_agent.placer.mcp import PlacerService
from eda_agent.placer.place.pipeline import solve

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_DIR = REPO_ROOT / "skills" / "electronics-ai-pro-pcb-designer"
SCHEMA_PATH = REPO_ROOT / "eda-agent" / "src" / "eda_agent" / "placer" / "intent" / "design_intent.schema.json"


def test_pro_pcb_designer_skill_frontmatter_and_reference_links():
    skill_md = SKILL_DIR / "SKILL.md"
    assert skill_md.is_file()
    text = skill_md.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    frontmatter = text.split("---", 2)[1]
    assert "name: electronics-ai-pro-pcb-designer" in frontmatter
    assert "description:" in frontmatter

    links = re.findall(r"\]\((references/[^)]+\.md)\)", text)
    assert len(links) >= 6
    assert "references/routing-silkscreen-and-visual-qa.md" in links
    assert "references/schematic-review-eco-and-fab-release.md" in links
    for rel in links:
        target = SKILL_DIR / rel
        assert target.is_file(), f"broken reference link in SKILL.md: {rel}"
        assert len(target.read_text(encoding="utf-8").strip()) > 500


def test_skill_silkscreen_enums_and_mandatory_visual_qa_gate():
    skill_md = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    checklist_md = (SKILL_DIR / "references" / "review-and-signoff-checklist.md").read_text(encoding="utf-8")
    qa_md = (SKILL_DIR / "references" / "routing-silkscreen-and-visual-qa.md").read_text(encoding="utf-8")
    fab_md = (SKILL_DIR / "references" / "schematic-review-eco-and-fab-release.md").read_text(encoding="utf-8")
    combined = f"{skill_md}\n{checklist_md}\n{qa_md}\n{fab_md}"

    # Every live silkscreen_policy and fab_profile enum must stay documented in the skill.
    for pol in silkscreen.SILKSCREEN_POLICIES:
        assert f"`{pol}`" in qa_md, f"silkscreen_policy `{pol}` missing from routing-silkscreen-and-visual-qa.md"
    for fab in silkscreen.FAB_PROFILES:
        assert f"`{fab}`" in qa_md, f"fab_profile `{fab}` missing from routing-silkscreen-and-visual-qa.md"

    # Mandatory visual QA gate (P9), IPC-7351 silkscreen, and industrial pre-fab release guards.
    assert "P9" in skill_md and "include_images=true" in skill_md
    assert "P9" in checklist_md and "include_images=true" in checklist_md
    assert "F.Fab" in combined and "B.Fab" in combined and "15" in combined and "25 um" in combined
    assert "--schematic-parity" in fab_md and "WAIVERS.md" in fab_md and "fiducials" in fab_md.lower()


def test_skill_tool_names_and_intent_schema_enums_match_live_code():
    all_md = "\n".join(p.read_text(encoding="utf-8") for p in sorted(SKILL_DIR.rglob("*.md")))
    mentioned = set(re.findall(r"\bplacer__([a-z][a-z0-9_]*)\b", all_md))
    live_tools = {t["name"]: set(t["inputSchema"]["properties"]) for t in PlacerService.list_tools()}
    assert mentioned == set(live_tools), f"skill/tool drift: extra={mentioned - set(live_tools)}, missing={set(live_tools) - mentioned}"

    # Verify every documented placer__<tool>(arg=...) parameter exists in live MCP inputSchema
    for tool_name, arg_str in re.findall(r"placer__([a-z][a-z0-9_]*)\(([^)]+)\)", all_md):
        for param in re.findall(r"\b([a-z][a-z0-9_]*)=", arg_str):
            assert param in live_tools[tool_name], (
                f"Skill documents unknown parameter `{param}` on `placer__{tool_name}`; "
                f"valid parameters: {sorted(live_tools[tool_name])}"
            )

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    contract_md = (SKILL_DIR / "references" / "intent-and-tool-contract.md").read_text(encoding="utf-8")
    for role in schema["$defs"]["role"]["enum"]:
        assert f"`{role}`" in contract_md, f"role missing from skill contract: {role}"
    for net_cls in schema["$defs"]["net"]["properties"]["class"]["allOf"][1]["properties"]["value"]["enum"]:
        assert f"`{net_cls}`" in contract_md, f"net class missing from skill contract: {net_cls}"
    for src_kind in schema["$defs"]["source"]["properties"]["kind"]["enum"]:
        assert src_kind in contract_md, f"source kind missing from skill contract: {src_kind}"
    for pair_kind in schema["$defs"]["pair_claim"]["allOf"][1]["properties"]["value"]["properties"]["kind"]["enum"]:
        assert f"`{pair_kind}`" in contract_md, f"pair kind missing from skill contract: {pair_kind}"
    for blk_kind in schema["$defs"]["block_claim"]["allOf"][1]["properties"]["value"]["properties"]["kind"]["enum"]:
        assert f"`{blk_kind}`" in contract_md, f"block kind missing from skill contract: {blk_kind}"


def _parity_board() -> Board:
    sq = [(-1.0, -0.6), (1.0, -0.6), (1.0, 0.6), (-1.0, 0.6)]

    def fp(ref, x, y, nets, locked=False):
        pads = [Pad(str(i + 1), n, (-0.5 + i, 0.0), (0.5, 0.5), layers=("F.Cu",)) for i, n in enumerate(nets)]
        return Footprint(ref, "v", "lib:x", x, y, 0.0, "F", locked=locked, pads=pads,
                         courtyard=Courtyard("file", [sq]), uuid="u-" + ref)

    fps = [
        fp("U1", 10.0, 10.0, ["+3V3", "GND", "SIG1", "SIG2"], locked=True),
        fp("C1", 4.0, 4.0, ["+3V3", "GND"]),
        fp("R1", 16.0, 4.0, ["SIG1", "SIG3"]),
        fp("R2", 16.0, 16.0, ["SIG2", "SIG3"]),
    ]
    b = Board(
        name="parity_board",
        footprints=fps,
        outline=[Outline([(0, 0), (20, 0), (20, 20), (0, 20)])],
        board_area_mm2=400.0,
        copper_layers=[CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("B.Cu", "B.Cu", 1)],
        netclasses=[NetClass("Default", 0.2, 0.2, 0.6, 0.3)],
        nets=[Net(n, "Default", 2) for n in ("+3V3", "GND", "SIG1", "SIG2", "SIG3")],
    )
    b.source = {"path": "parity_board.kicad_pcb", "sha256": "0" * 64}
    return derive(b)


def test_cross_client_intent_parity_yields_identical_hash_and_placement(tmp_path: Path):
    """Same semantic intent serialized by different clients (reversed key order, different questions) -> identical output (P7)."""
    board = _parity_board()
    base_intent = infer.draft(board)
    base_intent["parts"]["C1"]["target"] = model.claim(
        {"ref": "U1", "net": "+3V3", "pins": ["1"]},
        "datasheet",
        0.95,
        ref="U1 datasheet p.12",
    )

    # Client A (Codex): original ordering, empty questions
    codex_intent = json.loads(json.dumps(base_intent))
    codex_intent["questions"] = []
    codex_path = tmp_path / "codex.design_intent.json"
    codex_path.write_text(json.dumps(codex_intent, sort_keys=True), encoding="utf-8")

    # Client B (Antigravity): reversed top-level/nested key & list order + extra informational question
    ag_intent = dict(reversed(list(json.loads(json.dumps(base_intent)).items())))
    ag_intent["parts"] = dict(reversed(list(ag_intent["parts"].items())))
    ag_intent["nets"] = dict(reversed(list(ag_intent.get("nets", {}).items())))
    ag_intent["diff_pairs"] = list(reversed(ag_intent.get("diff_pairs", [])))
    ag_intent["blocks"] = list(reversed(ag_intent.get("blocks", [])))
    ag_intent["questions"] = [{"kind": "info", "note": "informational note excluded from canonical hash"}]
    ag_path = tmp_path / "antigravity.design_intent.json"
    ag_path.write_text(json.dumps(ag_intent, sort_keys=False), encoding="utf-8")

    i_codex = model.load(codex_path)
    i_ag = model.load(ag_path)
    v_codex = validate.validate(i_codex, board)
    v_ag = validate.validate(i_ag, board)
    assert not v_codex["errors"] and not v_ag["errors"]
    assert v_codex["intent_sha256"] == v_ag["intent_sha256"]

    p_codex, info_codex = solve(board, side_policy="expert", mode="best", seed=0, intent=i_codex, floorplan=True)
    p_ag, info_ag = solve(board, side_policy="expert", mode="best", seed=0, intent=i_ag, floorplan=True)
    assert p_codex.placement() == p_ag.placement()
    assert info_codex["strict_violations"] == 0 and info_ag["strict_violations"] == 0
    ev_codex = evaluate(p_codex, fixed=set(info_codex["fixed"]), intent=i_codex)
    ev_ag = evaluate(p_ag, fixed=set(info_ag["fixed"]), intent=i_ag)
    assert ev_codex["pro_score"] == ev_ag["pro_score"]
    assert ev_codex["metrics"]["pathfinder"] == ev_ag["metrics"]["pathfinder"]
