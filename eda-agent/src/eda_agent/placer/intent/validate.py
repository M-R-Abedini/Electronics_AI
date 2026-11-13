# SPDX-License-Identifier: MIT
"""``placer__intent_validate``: schema, references and sources of an intent against its board.

Errors make the intent unusable (the engine refuses it).  Warnings are
reported but do not block: a part without a claim falls back to the draft, a
board hash mismatch means the intent was written for another revision of the
file (still usable if every reference resolves, as when only copper moved).
"""

from __future__ import annotations

import json
from functools import cache

import jsonschema

from ..ir import Board
from . import infer, model


@cache
def _validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(json.loads(model.SCHEMA_FILE.read_text(encoding="utf-8")))


def validate(intent: dict, board: Board) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    if (board.source.get("format") == "altium_saved_intent" and board.footprints
            and not any(pad.net for fp in board.footprints for pad in fp.pads)):
        errors.append("native Altium snapshot has no decoded component-pad net connections")
    for err in sorted(_validator().iter_errors(intent), key=lambda e: list(map(str, e.absolute_path))):
        where = "/".join(map(str, err.absolute_path)) or "(root)"
        errors.append(f"schema {where}: {err.message}")
    if errors:
        return {"valid": False, "errors": errors, "warnings": warnings}

    fps = {fp.ref: fp for fp in board.footprints}
    nets = {n.name for n in board.nets} | {p.net for fp in board.footprints for p in fp.pads if p.net}
    if intent["board"]["sha256"] != board.source.get("sha256"):
        warnings.append("board sha256 differs from the intent's: written for another revision of the file")
    roles = {ref: part["role"]["value"] for ref, part in intent["parts"].items()}
    regions = {area["uuid"]: area for area in board.rule_areas}
    layer_names = {layer.name: layer.canonical for layer in board.copper_layers}
    for ref, constraint in intent.get("mechanical", {}).items():
        value = constraint["value"]
        if ref not in fps:
            errors.append(f"mechanical/{ref}: no such footprint")
        if not model.is_hard(constraint) or constraint["source"].get("kind") not in ("board", "user", "datasheet"):
            errors.append(f"mechanical/{ref}: requires confirmed or high-confidence mechanical evidence")
        if value["mode"] == "region":
            region = regions.get(value["region"])
            if not region or region["keepout"].get("footprints"):
                errors.append(f"mechanical/{ref}: region must be an existing rule area that allows footprints")
            elif ref in fps and not any(layer_names.get(layer, layer) == f"{fps[ref].side}.Cu" for layer in region["layers"]):
                errors.append(f"mechanical/{ref}: region does not cover the footprint side")
    for ref, part in intent["parts"].items():
        if ref not in fps:
            errors.append(f"parts/{ref}: no such reference on the board")
            continue
        target = part.get("target")
        if not target:
            if part["role"]["value"] in ("decap", "crystal_load_cap", "esd", "converter_inductor") \
                    and not model.is_uncertain(part["role"]):
                warnings.append(f"parts/{ref}: role {part['role']['value']} without a target")
            continue
        tv = target["value"]
        tref = tv.get("ref")
        for cand in ([tref] if tref else []) + list(tv.get("candidates", [])):
            if cand not in fps:
                errors.append(f"parts/{ref}/target: {cand} is not on the board")
        if tref and tref in fps:
            allowed = model.TARGET_ROLES.get(part["role"]["value"])
            if allowed and roles.get(tref) and roles[tref] not in allowed:
                errors.append(f"parts/{ref}/target: {tref} has role {roles[tref]}, "
                              f"a {part['role']['value']} serves {' or '.join(allowed)}")
            pads = {str(p.number) for p in fps[tref].pads}
            for pin in tv.get("pins", []):
                if pin not in pads:
                    errors.append(f"parts/{ref}/target: {tref} has no pin {pin}")
        net = tv.get("net")
        if net and net not in nets:
            errors.append(f"parts/{ref}/target: no net {net}")
        elif net and not any(p.net == net for p in fps[ref].pads):
            errors.append(f"parts/{ref}/target: {ref} has no pad on {net}")
    for net in intent.get("nets", {}):
        if net not in nets:
            errors.append(f"nets/{net}: no such net")
    for pair in intent.get("diff_pairs", []):
        for key in ("positive", "negative"):
            if pair["value"][key] not in nets:
                errors.append(f"diff_pairs: no net {pair['value'][key]}")
    for block in intent.get("blocks", []):
        for m in block["value"]["members"]:
            if m not in fps:
                errors.append(f"blocks/{block['value']['name']}: {m} is not on the board")
    for ref in intent.get("power_w", {}):
        if ref not in fps:
            errors.append(f"power_w/{ref}: no such reference")
    unclaimed = sorted(set(fps) - set(intent["parts"]), key=model.natural_key)
    if unclaimed:
        warnings.append(f"{len(unclaimed)} parts without a role claim (the draft's role applies): "
                        + " ".join(unclaimed[:20]) + (" ..." if len(unclaimed) > 20 else ""))

    claims = [p["role"] for p in intent["parts"].values()] + [p["target"] for p in intent["parts"].values()
                                                               if p.get("target")]
    claims += intent.get("diff_pairs", []) + intent.get("blocks", []) + list(intent.get("mechanical", {}).values())
    by_source: dict[str, int] = {}
    for c in claims:
        by_source[c["source"]["kind"]] = by_source.get(c["source"]["kind"], 0) + 1
    return {
        "valid": not errors, "errors": errors, "warnings": warnings,
        "intent_sha256": model.intent_hash(intent),
        "claims": {"total": len(claims), "hard": sum(model.is_hard(c) for c in claims),
                   "uncertain": sum(model.is_uncertain(c) for c in claims),
                   "confirmed_by_user": sum(bool(c.get("confirmed_by_user")) for c in claims),
                   "by_source": dict(sorted(by_source.items()))},
        "questions": infer.questions(intent),
    }
