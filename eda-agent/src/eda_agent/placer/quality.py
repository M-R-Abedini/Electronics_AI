# SPDX-License-Identifier: MIT
"""Evidence and acceptance of saved KiCad placement and routing artifacts."""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import replace
from pathlib import Path

from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from .eval import legality, metrics
from .intent import model
from .io.kicad import SIDECARS
from .ir import Board
from .place import escape, mechanical, problem
from .toolchain import sha256_file

MAX_REPAIRS = 3
_AREA_EPSILON = 1e-5
_AUTHORED = {"board", "user", "datasheet"}


def identity(path: Path) -> dict:
    return {"path": str(path.resolve()), "sha256": sha256_file(path),
            "sidecars": {suffix: sha256_file(path.with_suffix(suffix))
                         for suffix in SIDECARS if path.with_suffix(suffix).is_file()}}


def polygon(rows: list[dict]):
    pieces = [Polygon(row["outer"], row.get("holes", ())) for row in rows if len(row["outer"]) >= 3]
    return unary_union([part if part.is_valid else part.buffer(0) for part in pieces])


def _zone_defs(rows):
    definitions = {}
    for row in rows:
        definition = {key: row[key] for key in ("net", "layers", "priority", "name", "locked", "keepout", "settings")
                      if key in row}
        if "layers" in definition:
            definition["layers"] = sorted(definition["layers"])
        outlines = row.get("outline_components")
        if outlines is None:
            outlines = [{"outer": outer} for outer in row.get("outline", row.get("polygons", []))]
        definition["geometry"] = polygon(outlines).normalize().wkb_hex
        definitions.setdefault(row["uuid"], []).append(json.dumps(definition, sort_keys=True))
    # KiCad legacy files can assign the same nil UUID to several copper zones.
    return {uuid: sorted(rows) for uuid, rows in definitions.items()}


def zone_changes(basis: Board, saved: Board, *, cold: bool = False, allowed_new_rule_areas=()) -> list[str]:
    before = _zone_defs(basis.rule_areas + ([] if cold else basis.zones))
    after = _zone_defs(saved.rule_areas + saved.zones)
    for row in saved.rule_areas:
        uuid = row["uuid"]
        if (uuid in allowed_new_rule_areas and uuid not in before and row.get("name", "").startswith("eai_mask_guard_")
                and row["keepout"].get("zones") and sum(row["keepout"].values()) == 1):
            after.pop(uuid)
    return sorted(uuid for uuid in before.keys() | after.keys() if before.get(uuid) != after.get(uuid))


def fill_state(board: Board) -> dict:
    layers = {}
    for zone in board.zones:
        for layer in zone.get("layers", ()):
            entry = layers.setdefault(layer, {"zones": 0, "components": 0, "isolated_islands": 0})
            entry["zones"] += 1
            shapes = zone.get("fill_geometry", {}).get(layer, [])
            entry["components"] += len(shapes)
            entry["isolated_islands"] += sum(bool(row["island"]) for row in shapes)
    unfilled = [row["uuid"] for row in board.zones if not row["filled"]]
    filled = [row["uuid"] for row in board.zones
              if row["filled"] or any(area > 0 for area in row.get("filled_area_mm2", {}).values())]
    return {"state": ("unfilled" if unfilled else "filled") if board.zones else "no_zones",
            "unfilled": unfilled, "filled": filled, "teardrops": board.teardrops, "by_layer": layers,
            "geometry": ("native" if board.zones and all("fill_geometry" in z for z in board.zones)
                         else "area_only" if board.zones else "not_applicable")}


def finding(kind, refs=(), *, location=None, layer=None, detail=None, severity="error", **data):
    points = ([{"x_mm": location[0], "y_mm": location[1], "layer": layer, "ref": refs[0] if refs else None}]
              if location is not None else [])
    return {"kind": kind, "refs": list(refs), "description": detail or kind.replace("_", " "),
            "severity": severity, "points": points, **data}


def _bodies(board):
    return {fp.ref: box(*fp.body_bounds_mm) if fp.body_bounds_mm else legality.courtyard_polygon(fp)
            for fp in board.footprints if fp.pads and fp.prefix not in ("FID", "MH")
            and "testpoint" not in fp.lib_id.lower() and "mountinghole" not in fp.lib_id.lower()}


def _artwork_key(row, legacy):
    if legacy:
        # Upgrading old drawings with no IDs allocates new UUIDs on every read.
        return row["side"], row["owner"], row["label"], tuple(round(value, 4) for value in row["bounds_mm"])
    return row["uuid"]


def _artwork_overlaps(board, bodies, *, authored=None, legacy=False):
    overlaps = {}
    for row in board.artwork:
        key = _artwork_key(row, legacy)
        if authored is not None and key not in authored:
            continue
        ink = box(*row["bounds_mm"])
        for fp in board.footprints:
            body = bodies.get(fp.ref)
            if fp.side != row["side"] or fp.ref == row["owner"] or body is None or not body.intersects(ink):
                continue
            cover = body.intersection(ink)
            if cover.area > _AREA_EPSILON:
                overlaps[key, fp.ref] = (cover.area, cover.centroid.coords[0], row)
    return overlaps


def _hard_claim(row):
    return model.is_hard(row) and row.get("source", {}).get("kind") in _AUTHORED


def _critical_distances(board, intent):
    """Pad-to-pad distances for authored critical links, evaluated at the saved pose."""
    by_ref = {fp.ref: fp for fp in board.footprints}
    by_net = board.net_pads()
    distances = {}
    for ref, part in intent.get("parts", {}).items():
        role, target = part.get("role", {}), part.get("target") or {}
        value = target.get("value", {})
        role_name = role.get("value")
        other = value.get("ref")
        if not (_hard_claim(target) and other in by_ref and ref in by_ref
                and (model.is_critical(role_name) or role_name in ("regulator_input_cap", "regulator_output_cap"))):
            continue
        a, b = by_ref[ref], by_ref[other]
        pins = set(value.get("pins", ()))
        distances[ref, other] = min(
            (math.dist(x, y) for p, x in a.pads_world() for q, y in b.pads_world()
             if p.net and p.net == q.net and (not value.get("net") or p.net == value["net"])
             and (not pins or q.number in pins)), default=math.inf)
    for net, claims in intent.get("nets", {}).items():
        claim = claims.get("class") or {}
        if not _hard_claim(claim) or claim.get("value") not in (
            "SW_NODE", "DIFF_PAIR", "HIGH_SPEED", "RF", "CLOCK", "SENSITIVE_ANALOG", "POWER_HIGH_CURRENT"
        ):
            continue
        pads = by_net.get(net, ())
        refs = tuple(sorted({fp.ref for fp, _ in pads}))
        if len(refs) > 1:
            distances[refs + (net,)] = metrics._hpwl([fp.pad_world(pad) for fp, pad in pads])
    return distances


class PlacementReview:
    """Compare every candidate against the same authored PCB and mechanical commitments."""

    def __init__(self, basis: Board, intent: dict, fixed: dict, anchors=(), *, cold=False, fab_profile="ipc_standard"):
        from . import silkscreen

        self.basis = basis
        self.intent = intent
        self.fixed = fixed
        self.cold = cold
        self.fab_profile = fab_profile
        self.source = {fp.ref: fp for fp in basis.footprints}
        self.interfaces = mechanical.interface_inventory(basis, intent)
        self.expected = {ref: self.source[ref] for ref in fixed if ref in self.source}
        self.expected.update({row["ref"]: self.source[row["ref"]] for row in self.interfaces if row["protected"]})
        for row in anchors:
            ref = row["ref"]
            x, y = row["position_mm"]
            self.expected[ref] = replace(self.source[ref], x=x, y=y, rotation=row["angle_deg"], side=row["side"])
        self.relocated = {row["ref"] for row in anchors if row["mode"] == "region"}
        self.legacy_artwork = (bool(basis.source.get("upgraded_from_legacy"))
                               or basis.source.get("format_version", 20200101) < 20200101)
        self.authored_artwork = Counter(_artwork_key(row, self.legacy_artwork) for row in basis.artwork)
        self.base_artwork = _artwork_overlaps(basis, _bodies(basis), legacy=self.legacy_artwork)
        self.base_legality = self._legal(basis)
        self.base_silk = self._silk_errors(silkscreen.audit_board(basis, fab_profile=fab_profile))
        self.critical = _critical_distances(basis, intent)
        self.envelopes = escape.envelopes(basis, intent, {fp.ref: problem.local_box(fp) for fp in basis.footprints})
        self.base_exits = self._blocked(basis)

    def _legal(self, board):
        return {(row.kind, tuple(row.refs), row.side, row.detail): row.area_mm2
                for row in legality.check(board, fixed=set(self.fixed)) if not row.fixed}

    def _blocked(self, board):
        flipped = {fp.ref for fp in board.footprints if fp.ref in self.source and fp.side != self.source[fp.ref].side}
        return {(row["ref"], row["pin"], row["net"]): row
                for row in escape.audit(board, self.envelopes, flipped=flipped).get("blocked", ())}

    @staticmethod
    def _silk_errors(silk):
        return {(row["rule"], row.get("ref"), row.get("layer")): row for row in silk.get("findings", [])
                if row.get("severity") == "error" and row["rule"] in (
                    "silk_text_overlap", "silk_under_component_body", "silk_pad_clearance", "silk_text_outside_board",
                    "fab_text_overlap", "fab_text_outside_board")}

    def compare(self, saved: Board, *, draft=True, silk=None, allowed_new_rule_areas=()):
        from . import silkscreen

        by_ref = {fp.ref: fp for fp in saved.footprints}
        bodies = _bodies(saved)
        outline = legality.board_polygon(saved)
        findings = []
        if not saved.outline_valid or outline is None:
            findings.append(finding("invalid_board_outline"))
        if zone_defs := zone_changes(self.basis, saved, cold=self.cold, allowed_new_rule_areas=allowed_new_rule_areas):
            findings.append(finding("zone_definition_changed", zones=zone_defs))
        if draft and (fill_state(saved)["filled"] or saved.teardrops):
            findings.append(finding("stale_zone_fill"))
        for ref, expected in self.expected.items():
            actual = by_ref.get(ref)
            if actual is None or not mechanical.same_pose(expected, actual):
                findings.append(finding("protected_pose_changed", (ref,), location=expected.position,
                                        layer=f"{expected.side}.Cu", actual=(list(actual.position) if actual else None)))
        for row in self.interfaces:
            ref, access = row["ref"], row["access"]
            fp = by_ref.get(ref)
            if row["status"] == "needs_evidence" and ref not in self.relocated:
                findings.append(finding("mechanical_evidence_missing", (ref,), location=tuple(row["pose"][:2]),
                                        layer=f"{row['pose'][3]}.Cu", severity="unknown", detail=row["reason"]))
            if fp is None or access is None or outline is None:
                continue
            corridor = mechanical.access_geometry(fp, access)
            for other in saved.footprints:
                body = bodies.get(other.ref)
                if (other.ref != ref and other.side == fp.side and body is not None
                        and corridor.intersection(body).area > _AREA_EPSILON):
                    findings.append(finding("mating_path_blocked", (ref, other.ref), location=other.position,
                                            layer=f"{fp.side}.Cu"))
        saved_artwork = Counter(_artwork_key(row, self.legacy_artwork) for row in saved.artwork)
        if missing_artwork := self.authored_artwork - saved_artwork:
            findings.append(finding("authored_artwork_missing", artwork=sorted(map(str, missing_artwork))))
        overlaps = _artwork_overlaps(saved, bodies, authored=self.authored_artwork, legacy=self.legacy_artwork)
        for (key, ref), (area, point, row) in overlaps.items():
            if area > self.base_artwork.get((key, ref), (0.0,))[0] + _AREA_EPSILON:
                findings.append(finding("artwork_occluded", (ref,), location=point, layer=f"{row['side']}.Silkscreen",
                                        artwork=str(key), label=row["label"], overlap_mm2=round(area, 6)))
        for (kind, refs, side, detail), area in self._legal(saved).items():
            baseline = self.base_legality.get((kind, refs, side, detail))
            if baseline is None or area > baseline + _AREA_EPSILON:
                fp = by_ref.get(refs[0]) if refs else None
                findings.append(finding("new_placement_violation", refs, location=fp.position if fp else None,
                                        layer=f"{side}.Cu" if side else None, detail=f"{kind}: {detail}"))
        for key, row in self._blocked(saved).items():
            if key in self.base_exits:
                continue
            ref, pin, net = key
            claim = self.intent.get("nets", {}).get(net, {}).get("class")
            if not (claim and model.is_hard(claim) and claim["value"] != "GENERAL"):
                continue
            findings.append(finding("critical_pad_exit_blocked", (ref, *row["blocked_by"]),
                                    location=row["start_mm"], layer=row["layer"], pad=pin, net=net))
        for refs, distance in _critical_distances(saved, self.intent).items():
            if distance > self.critical.get(refs, math.inf) + mechanical.POSE_MM:
                fp = by_ref[refs[0]]
                findings.append(finding("authored_critical_link_regressed", tuple(ref for ref in refs if ref in by_ref),
                                        location=fp.position, layer=f"{fp.side}.Cu",
                                        old_mm=self.critical[refs], new_mm=distance if math.isfinite(distance) else None))
        silk = silk if silk is not None else silkscreen.audit_board(saved, fab_profile=self.fab_profile)
        for key, row in self._silk_errors(silk).items():
            if key not in self.base_silk:
                refs = (row["ref"],) if row.get("ref") else ()
                findings.append(finding(row["rule"], refs, location=row.get("pos_mm"), layer=row.get("layer"),
                                        severity="warning"))
        return findings


def rank(findings):
    return (sum(row["severity"] == "error" for row in findings),
            sum(row["severity"] == "unknown" for row in findings))


def improves(before, after):
    def causes(rows):
        return {(row["kind"], tuple(row["refs"]), str(row.get("artwork"))) for row in rows if row["severity"] == "error"}

    return rank(after) < rank(before) and not (causes(after) - causes(before))


def routing_status(metrics, gates, copper, *, reviewed=True):
    failed = [gate["name"] for gate in gates if not gate["passed"]]
    if copper["unfilled"]:
        failed.append("zones_not_filled")
    if not reviewed:
        failed.append("final_review_failed")
    open_links = metrics.get("unrouted")
    router_stop = metrics.get("router_stop")
    reasons = list(failed)
    if not metrics.get("settled") or router_stop == "timeout":
        reasons.append("router_unsettled")
    if open_links != 0:
        reasons.append("open_connections")
    if metrics.get("drc_track_dangling") != 0 or metrics.get("drc_via_dangling") != 0:
        reasons.append("dangling_copper")
    return {"status": "failed" if failed else "trial" if reasons else "approved", "reasons": reasons,
            "unrouted": open_links, "copper": copper}


def write_findings(path: Path, findings: list[dict]) -> Path:
    path.write_text(json.dumps(findings, indent=1, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    return path
