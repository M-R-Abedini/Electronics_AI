#!/usr/bin/env python3
"""Merge an audio-engineering-skill handoff into a circuit design spec.

The audio skill owns *what* must be achieved.  This script maps its versioned
handoff into the circuit skill's ``design`` and ``metrics`` blocks.  It needs a
base circuit spec (the electrical implementation and its analyses) and a small
mapping file that supplies the electrical test points the audio handoff cannot
know: output/input rawfile variables, test frequencies and generated metric ids.

The merged spec is validated with ``design_spec.validate_spec``.  A missing
analysis, an unsupported measure, a metric without a target or limit, or a
metric that does not name an existing requirement is an error.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import design_spec  # noqa: E402

HANDOFF_VERSION = 1
AUDIO_SKILL_ENV = "AUDIO_ENGINEERING_SKILL"


class MergeError(ValueError):
    """A handoff, base spec or mapping cannot be merged safely."""


def _load(path: str | Path, label: str) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise MergeError(f"{label} {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MergeError(f"{label} {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise MergeError(f"{label} {path}: top level must be an object")
    return data


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _metric_key(metric: dict[str, Any]) -> tuple[Any, Any]:
    return metric.get("measure"), metric.get("requirement")


def _require_metric_limits(metric: dict[str, Any], where: str) -> None:
    has_target = "target" in metric
    has_limits = "max" in metric or "min" in metric
    exploratory = metric.get("exploratory") is True
    if not has_target and not has_limits and not exploratory:
        raise MergeError(
            f"{where}: needs target+tolerance, min, max, or exploratory=true")
    if has_target and "tolerance" not in metric:
        raise MergeError(f"{where}: target needs a tolerance")
    if has_target and not isinstance(metric["tolerance"], (int, float)):
        raise MergeError(f"{where}: tolerance must be a number")


def _merge_by_id(existing: Any, incoming: Any, label: str) -> list[dict[str, Any]]:
    if existing is None:
        existing = []
    if not isinstance(existing, list) or not all(isinstance(item, dict) for item in existing):
        raise MergeError(f"base design.{label} must be a list of objects")
    if not isinstance(incoming, list):
        raise MergeError(f"handoff.{label} must be a list")
    result = list(existing)
    index_by_id = {
        item["id"]: index for index, item in enumerate(result)
        if isinstance(item.get("id"), str)
    }
    for item in incoming:
        if not isinstance(item, dict):
            raise MergeError(f"handoff.{label} entries must be objects")
        identifier = item.get("id")
        if isinstance(identifier, str) and identifier in index_by_id:
            result[index_by_id[identifier]] = copy.deepcopy(item)
        else:
            if isinstance(identifier, str):
                index_by_id[identifier] = len(result)
            result.append(copy.deepcopy(item))
    return result


def find_audio_skill(explicit: str | Path | None = None) -> Path | None:
    """Locate the sibling audio skill, or return ``None``.

    Search order: explicit path, ``AUDIO_ENGINEERING_SKILL``, the parent of
    this repository, then the current directory.  A candidate counts only when
    it has ``catalog/sources.json``.
    """
    candidates: list[Path] = []
    if explicit:
        candidate = Path(explicit)
        if (candidate / "catalog" / "sources.json").is_file():
            return candidate.resolve()
        return None
    env = os.environ.get(AUDIO_SKILL_ENV)
    if env:
        candidates.append(Path(env))
    repo_root = Path(__file__).resolve().parents[1]
    for base in (repo_root.parent, Path.cwd()):
        candidates.append(base / "audio-engineering-skill")
    for candidate in candidates:
        if (candidate / "catalog" / "sources.json").is_file():
            return candidate.resolve()
    return None


def load_audio_registry(skill: Path) -> dict[str, dict[str, Any]]:
    """Load the audio skill's source registry, keyed by source id."""
    path = skill / "catalog" / "sources.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MergeError(f"audio registry {path}: {exc}") from exc
    entries = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise MergeError(f"audio registry {path}: sources must be a list")
    registry: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise MergeError(
                f"audio registry {path}: sources[{index}] must be an object")
        sid = entry.get("id")
        if not isinstance(sid, str) or not sid:
            raise MergeError(
                f"audio registry {path}: sources[{index}] needs an id")
        registry[sid] = entry
    return registry


def verify_sources(handoff: dict[str, Any],
                   registry: dict[str, dict[str, Any]]) -> None:
    """Require every cited and listed source to be verified in the registry.

    This mirrors ``source_check.py --handoff`` in the audio skill so an
    unvalidated handoff cannot pass the merge with unverified provenance.
    """
    listed = handoff.get("sources")
    if not isinstance(listed, list) or not listed:
        raise MergeError("handoff.sources must be a non-empty list")
    listed_ids: set[str] = set()
    for index, source in enumerate(listed):
        where = f"handoff.sources[{index}]"
        if not isinstance(source, dict):
            raise MergeError(f"{where} must be an object")
        sid = source.get("id")
        if not isinstance(sid, str) or not sid:
            raise MergeError(f"{where}: id must be a non-empty string")
        if sid in listed_ids:
            raise MergeError(f"{where}: duplicate source id {sid}")
        listed_ids.add(sid)
        entry = registry.get(sid)
        if entry is None:
            raise MergeError(f"{where}: unknown source {sid}")
        if entry.get("status") != "verified":
            raise MergeError(
                f"{where}: source {sid} is {entry.get('status')!r}, "
                f"not verified")

    requirements = handoff.get("requirements")
    if not isinstance(requirements, list):
        raise MergeError("handoff.requirements must be a list")
    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, dict):
            continue
        sid = requirement.get("source")
        if not isinstance(sid, str) or not sid:
            continue
        if sid not in listed_ids:
            raise MergeError(
                f"handoff.requirements[{index}]: source {sid} is not listed "
                f"in handoff.sources")
        entry = registry.get(sid)
        if entry is None:
            raise MergeError(
                f"handoff.requirements[{index}]: unknown source {sid}")
        if entry.get("status") != "verified":
            raise MergeError(
                f"handoff.requirements[{index}]: source {sid} is "
                f"{entry.get('status')!r}, not verified")


def merge(handoff: dict[str, Any], base: dict[str, Any],
          mapping: dict[str, Any],
          registry: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return a circuit design spec built from the three inputs.

    The base spec supplies components, analyses and deliverables.  The handoff
    supplies requirements, conditions, architecture, risks, sources and limits.
    The mapping supplies the electrical test points for each handoff metric.
    When ``registry`` is supplied, every cited source must be verified there.
    """
    if not isinstance(handoff, dict):
        raise MergeError("handoff must be an object")
    if handoff.get("handoff_version") != HANDOFF_VERSION:
        raise MergeError(
            f"handoff_version must be {HANDOFF_VERSION}, got "
            f"{handoff.get('handoff_version')!r}")
    if not _nonempty(handoff.get("application")):
        raise MergeError("handoff.application is required")
    if not isinstance(base, dict) or not _nonempty(base.get("name")):
        raise MergeError("base spec must be an object with a non-empty name")
    if registry is not None:
        verify_sources(handoff, registry)

    spec = copy.deepcopy(base)
    design = dict(spec.get("design") or {})

    # Requirements: merge by id, keeping base-only requirements.
    handoff_requirements = handoff.get("requirements")
    if not isinstance(handoff_requirements, list) or not handoff_requirements:
        raise MergeError("handoff.requirements must be a non-empty list")
    requirements = list(design.get("requirements") or [])
    if not all(isinstance(item, dict) for item in requirements):
        raise MergeError("base design.requirements must contain objects")
    by_id = {
        item["id"]: index for index, item in enumerate(requirements)
        if isinstance(item.get("id"), str)
    }
    for index, requirement in enumerate(handoff_requirements):
        where = f"handoff.requirements[{index}]"
        if not isinstance(requirement, dict):
            raise MergeError(f"{where} must be an object")
        for field in ("id", "text", "acceptance", "source"):
            if not _nonempty(requirement.get(field)):
                raise MergeError(f"{where}.{field} is required")
        rid = requirement["id"]
        if rid in by_id:
            requirements[by_id[rid]] = copy.deepcopy(requirement)
        else:
            by_id[rid] = len(requirements)
            requirements.append(copy.deepcopy(requirement))
    design["requirements"] = requirements

    if handoff.get("architecture") is not None:
        if not isinstance(handoff["architecture"], dict):
            raise MergeError("handoff.architecture must be an object")
        design["architecture"] = copy.deepcopy(handoff["architecture"])
    for key in ("risks", "sources"):
        if handoff.get(key) is not None:
            design[key] = _merge_by_id(design.get(key), handoff[key], key)
    spec["design"] = design

    # Metrics: each handoff metric needs a mapping entry with the electrical
    # test point.  Match by (measure, requirement), not by list position.
    handoff_metrics = handoff.get("metrics")
    if not isinstance(handoff_metrics, list) or not handoff_metrics:
        raise MergeError("handoff.metrics must be a non-empty list")
    map_metrics = mapping.get("metrics")
    if not isinstance(map_metrics, list) or not map_metrics:
        raise MergeError("mapping.metrics must be a non-empty list")
    mapping_by_key: dict[tuple[Any, Any], dict[str, Any]] = {}
    for index, entry in enumerate(map_metrics):
        where = f"mapping.metrics[{index}]"
        if not isinstance(entry, dict):
            raise MergeError(f"{where} must be an object")
        measure = entry.get("measure")
        if measure not in design_spec.METRIC_MEASURES:
            raise MergeError(f"{where}: unsupported measure {measure!r}")
        key = (measure, entry.get("requirement"))
        if key in mapping_by_key:
            raise MergeError(f"{where}: duplicate mapping for {key!r}")
        mapping_by_key[key] = entry

    merged_metrics: list[dict[str, Any]] = []
    used_mappings: set[tuple[Any, Any]] = set()
    for index, metric in enumerate(handoff_metrics):
        where = f"handoff.metrics[{index}]"
        if not isinstance(metric, dict):
            raise MergeError(f"{where} must be an object")
        measure = metric.get("measure")
        if measure not in design_spec.METRIC_MEASURES:
            raise MergeError(f"{where}: unsupported measure {measure!r}")
        requirement = metric.get("requirement")
        if requirement is not None and requirement not in by_id:
            raise MergeError(
                f"{where}: requirement {requirement!r} is absent from the "
                f"merged requirements")
        if requirement is None and metric.get("exploratory") is not True:
            raise MergeError(
                f"{where}: a non-exploratory metric needs a requirement")
        _require_metric_limits(metric, where)
        key = _metric_key(metric)
        entry = mapping_by_key.get(key)
        if entry is None:
            raise MergeError(
                f"{where}: no mapping entry for measure={measure!r}, "
                f"requirement={requirement!r}")
        if key in used_mappings:
            raise MergeError(f"{where}: mapping already used for {key!r}")
        used_mappings.add(key)

        expected_analysis = design_spec.METRIC_ANALYSIS[measure]
        analysis = entry.get("analysis", metric.get("analysis", expected_analysis))
        if analysis != expected_analysis:
            raise MergeError(
                f"{where}: {measure} uses the {expected_analysis!r} analysis, "
                f"not {analysis!r}")
        output = entry.get("output", metric.get("output"))
        if not _nonempty(output):
            raise MergeError(
                f"{where}: mapping needs 'output' (a rawfile variable such as "
                f"'v(out)')")
        merged: dict[str, Any] = {
            "id": entry.get("id") or f"M{len(merged_metrics) + 1}",
            "measure": measure,
            "analysis": analysis,
            "output": output,
        }
        if requirement is not None:
            merged["requirement"] = requirement
        if measure in ("gain_db", "bandwidth_hz", "bandwidth_low_hz",
                       "bandwidth_width_hz"):
            source = entry.get("input", metric.get("input"))
            if not _nonempty(source):
                raise MergeError(
                    f"{where}: mapping needs 'input' (a rawfile variable such "
                    f"as 'v(in)') for {measure}")
            merged["input"] = source
        for field in ("frequency", "fundamental", "start", "stop", "harmonics",
                      "unit"):
            if field in entry:
                merged[field] = entry[field]
            elif field in metric:
                merged[field] = metric[field]
        for field in ("target", "tolerance", "min", "max", "exploratory"):
            if field in metric:
                merged[field] = metric[field]
        # Preserve audio-specific context (bandwidth_hz, passband, weighting,
        # detector, etc.) for the record.  The circuit validator ignores fields
        # it does not use.
        for field, value in metric.items():
            if field not in merged and field not in ("measure", "requirement"):
                merged[field] = copy.deepcopy(value)
        merged_metrics.append(merged)

    conditions = dict((spec.get("metrics") or {}).get("conditions") or {})
    handoff_conditions = handoff.get("conditions")
    if isinstance(handoff_conditions, dict):
        conditions.update(copy.deepcopy(handoff_conditions))
    spec["metrics"] = {"conditions": conditions, "metrics": merged_metrics}
    return spec


def validate_merged(spec: dict[str, Any], base_dir: str | Path | None = None) -> dict[str, Any]:
    return design_spec.validate_spec(spec, base_dir=base_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handoff", required=True, help="audio handoff JSON")
    parser.add_argument("--base", required=True,
                        help="base circuit spec with components and analyses")
    parser.add_argument("--map", dest="mapping", required=True,
                        help="metric mapping JSON")
    parser.add_argument("--out", required=True, help="merged spec JSON")
    parser.add_argument("--audio-skill",
                        help="path to the audio-engineering-skill directory")
    parser.add_argument("--require-registry", action="store_true",
                        help="fail when the audio registry cannot be found")
    args = parser.parse_args(argv)
    try:
        handoff = _load(args.handoff, "handoff")
        base = _load(args.base, "base spec")
        mapping = _load(args.mapping, "mapping")
        skill = find_audio_skill(args.audio_skill)
        if args.audio_skill and skill is None:
            raise MergeError(
                f"audio skill {args.audio_skill}: catalog/sources.json not found")
        if skill is None:
            if args.require_registry:
                raise MergeError(
                    f"--require-registry: audio-engineering-skill not found; "
                    f"pass --audio-skill or set {AUDIO_SKILL_ENV}")
            print(f"WARN  [audio] sibling audio-engineering-skill not found; "
                  f"source ids were not checked against its registry",
                  file=sys.stderr)
            registry = None
        else:
            registry = load_audio_registry(skill)
        merged = merge(handoff, base, mapping, registry=registry)
        report = validate_merged(merged, Path(args.base).resolve().parent)
        if not report["ok"]:
            for error in report["errors"]:
                print(f"error: {error}", file=sys.stderr)
            return 1
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out}: {len(merged['metrics']['metrics'])} metrics, "
              f"{len(merged['design']['requirements'])} requirements")
        return 0
    except MergeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
