# SPDX-License-Identifier: MIT
"""F7: Safe, transactional, reversible EDA apply & revert (architecture P8 & section 9).

Default behavior never modifies the user's original board (P8): ``apply_run``
writes the chosen run's verified output to a new sibling file (or explicit
``target_path``).  Explicit ``in_place=True`` performs a strict transactional
update:

1. Gate check: refuses runs that failed legality/DRC gates unless opted in.
2. Stale-target check: compares the target board's live SHA-256 against the
   run's recorded input SHA-256 so concurrent manual edits are never clobbered.
3. Hermetic backup: copies target + sidecars into ``apply_history/<apply_id>/backup``
   and verifies every backup SHA-256 BEFORE touching the target.
4. Atomic swap: writes to a temp file in the target's own directory, verifies
   its SHA-256, and swaps via ``os.replace``.
5. Instant revert (``< 2 s`` gate): ``revert_apply`` verifies the target has
   not been edited since apply, atomically restores the verified backup, and
   proves byte-for-byte restoration via SHA-256.

Adapters:
- ``kicad_file`` (default): file-based KiCad apply/revert.
- ``altium_plan``: generates a deterministic Level-1 (no-flip by default)
  apply + inverse revert plan bundle compatible with the Altium safe-apply bridge.
- ``kicad_ipc``: optional live KiCad GUI IPC (explicitly checks availability and
  never silently falls back).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .io.kicad import SIDECARS, read_board
from .toolchain import RUNTIME_DIR, sha256_file

SCHEMA = "electronics-ai/apply_manifest"
SCHEMA_VERSION = 1
ADAPTERS = ("kicad_file", "altium_plan", "kicad_ipc")
REVERT_BUDGET_S = 2.0


class ApplyError(RuntimeError):
    """Raised when an apply or revert safety precondition fails."""


def _default_history_dir(history_dir: str | Path | None) -> Path:
    return (Path(history_dir) if history_dir is not None else RUNTIME_DIR / "apply_history").resolve()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


def _atomic_copy(src: Path, dst: Path, *, expected_sha256: str | None = None) -> str:
    """Copy ``src`` -> ``dst`` atomically in ``dst.parent`` with SHA-256 verification."""
    src = src.resolve()
    dst = dst.resolve()
    expected = expected_sha256 or sha256_file(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.parent / f".{dst.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp"
    try:
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            shutil.copyfileobj(fin, fout, length=1024 * 1024)
            fout.flush()
            os.fsync(fout.fileno())
        got = sha256_file(tmp)
        if got != expected:
            raise ApplyError(f"integrity check failed while writing {dst.name}: expected {expected[:12]}, got {got[:12]}")
        os.replace(tmp, dst)
        return got
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


def resolve_run(run: str | Path, *, use_routed: bool = True) -> dict[str, Any]:
    """Locate ``run_report.json``, source board metadata, and best output board artifact."""
    p = Path(run).resolve()
    report_path = p / "run_report.json" if p.is_dir() else p
    if not report_path.is_file():
        raise ApplyError(f"run_report.json not found: {report_path}")
    rep = json.loads(report_path.read_text(encoding="utf-8"))
    run_dir = Path((rep.get("artifacts") or {}).get("run_dir") or report_path.parent).resolve()

    board_input = (rep.get("inputs") or {}).get("board") or {}
    input_path_raw = board_input.get("path")
    if not input_path_raw:
        raise ApplyError(f"run report missing inputs.board.path: {report_path}")
    input_path = Path(input_path_raw).resolve()
    input_sha = board_input.get("sha256")

    artifacts = rep.get("artifacts") or {}
    placed_raw = artifacts.get("placed_board")
    routed_raw = artifacts.get("routed_board")
    if routed_raw is None:
        route_rep_path = run_dir / "route" / "run_report.json"
        if route_rep_path.is_file():
            route_rep = json.loads(route_rep_path.read_text(encoding="utf-8"))
            routed_raw = (route_rep.get("artifacts") or {}).get("routed_board")

    artifact_kind: str | None = None
    artifact_path: Path | None = None
    checked_artifact = None
    quality_report = rep.get("quality")
    if quality_report is not None:
        from .quality import identity

        reviewed_source = (rep.get("inputs") or {}).get("board_artifact")
        if reviewed_source and (not input_path.is_file() or reviewed_source != identity(input_path)):
            raise ApplyError("source board or project rules changed since review")
        artifact_kind = "routed" if use_routed else "placed"
        checked = quality_report.get("routing" if use_routed else "placement") or {}
        if checked.get("status") != "approved":
            raise ApplyError(f"{artifact_kind} output has no current approval ({checked.get('status', 'unreviewed')})")
        selected = routed_raw if use_routed else placed_raw
        if not selected or not Path(selected).is_file():
            raise ApplyError(f"approved {artifact_kind} artifact is missing")
        artifact_path = Path(selected).resolve()
        checked_artifact = checked.get("artifact")
        if checked_artifact != identity(artifact_path):
            raise ApplyError(f"{artifact_kind} artifact or its project rules changed since review")
    elif board_input.get("eda") == "kicad" or "kicad_pro" in (rep.get("inputs") or {}):
        raise ApplyError("legacy design run requires a fresh review before apply")
    elif use_routed and routed_raw and Path(routed_raw).is_file():
        artifact_kind = "routed"
        artifact_path = Path(routed_raw).resolve()
    elif placed_raw and Path(placed_raw).is_file():
        artifact_kind = "placed"
        artifact_path = Path(placed_raw).resolve()
    elif routed_raw and Path(routed_raw).is_file():
        artifact_kind = "routed"
        artifact_path = Path(routed_raw).resolve()

    if artifact_path is None or artifact_kind is None:
        raise ApplyError(f"no placed or routed board artifact found for run: {report_path}")

    return {
        "run_report_path": report_path,
        "run_dir": run_dir,
        "report": rep,
        "input_board_path": input_path,
        "input_board_sha256": input_sha,
        "artifact_kind": artifact_kind,
        "artifact_path": artifact_path,
        "artifact_sha256": checked_artifact["sha256"] if checked_artifact else sha256_file(artifact_path),
        "artifact_sidecars": checked_artifact["sidecars"] if checked_artifact else None,
    }


def _check_kicad_ipc() -> None:
    try:
        import kipy  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ApplyError(
            "kicad_ipc adapter requires the optional `kicad-python` (`kipy`) package and a running KiCad 10 GUI "
            "with IPC API enabled; use adapter='kicad_file' (default) for file-based apply."
        ) from exc
    try:
        client = kipy.KiCad()
        client.ping()
    except Exception as exc:
        raise ApplyError(
            f"KiCad live IPC connection failed ({type(exc).__name__}: {exc}); "
            "open KiCad with IPC enabled or use adapter='kicad_file'."
        ) from exc


def _build_altium_plan_bundle(
    resolved: dict[str, Any],
    bundle_dir: Path,
    *,
    allow_flip: bool,
) -> dict[str, Any]:
    """Generate deterministic Level-1 (no-flip) Altium apply + revert delta plans."""
    src_board = read_board(resolved["input_board_path"])
    dst_board = read_board(resolved["artifact_path"])

    apply_ops: list[dict[str, Any]] = []
    revert_ops: list[dict[str, Any]] = []
    flipped_refs: list[str] = []

    for fp_new in sorted(dst_board.footprints, key=lambda f: f.ref):
        fp_old = src_board.fp(fp_new.ref)
        if fp_new.side != fp_old.side:
            flipped_refs.append(fp_new.ref)
        moved = (
            abs(fp_new.x - fp_old.x) > 1e-4
            or abs(fp_new.y - fp_old.y) > 1e-4
            or abs((fp_new.rotation - fp_old.rotation) % 360.0) > 1e-3
            or fp_new.side != fp_old.side
        )
        if not moved:
            continue
        # Altium uses +Y up while KiCad uses +Y down.
        apply_ops.append({
            "ref": fp_new.ref,
            "x_mm": round(fp_new.x, 4),
            "y_mm": round(-fp_new.y, 4),
            "rotation_deg": round(fp_new.rotation % 360.0, 3),
            "layer": "Top" if fp_new.side == "F" else "Bottom",
        })
        revert_ops.append({
            "ref": fp_old.ref,
            "x_mm": round(fp_old.x, 4),
            "y_mm": round(-fp_old.y, 4),
            "rotation_deg": round(fp_old.rotation % 360.0, 3),
            "layer": "Top" if fp_old.side == "F" else "Bottom",
        })

    if flipped_refs and not allow_flip:
        raise ApplyError(
            f"altium_plan enforces assembly level 1 (no side flip) by default; "
            f"{len(flipped_refs)} flipped footprints blocked: {', '.join(flipped_refs[:10])}. "
            "Pass allow_flip=True only after flip verification."
        )

    bundle_dir.mkdir(parents=True, exist_ok=True)
    apply_plan_path = bundle_dir / "altium_apply_plan.json"
    revert_plan_path = bundle_dir / "altium_revert_plan.json"
    precondition = {
        "input_board_path": str(resolved["input_board_path"]),
        "input_board_sha256": resolved["input_board_sha256"],
        "assembly_level": 2 if allow_flip else 1,
    }
    _atomic_write_text(
        apply_plan_path,
        json.dumps({"kind": "altium_apply_plan", "precondition": precondition, "operations": apply_ops},
                   indent=1, sort_keys=True),
    )
    _atomic_write_text(
        revert_plan_path,
        json.dumps({"kind": "altium_revert_plan", "precondition": precondition, "operations": revert_ops},
                   indent=1, sort_keys=True),
    )
    return {
        "operations": len(apply_ops),
        "flipped_refs": flipped_refs,
        "apply_plan": str(apply_plan_path),
        "revert_plan": str(revert_plan_path),
    }


def apply_run(
    run: str | Path,
    *,
    history_dir: str | Path | None = None,
    adapter: str = "kicad_file",
    target_path: str | Path | None = None,
    in_place: bool = False,
    use_routed: bool = True,
    allow_failed_gates: bool = False,
    force_stale: bool = False,
    allow_flip: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply a verified placement/routing run safely with full rollback metadata."""
    t0 = time.monotonic()
    if adapter not in ADAPTERS:
        raise ValueError(f"unknown adapter {adapter!r}; expected one of {ADAPTERS}")
    if in_place and target_path is not None:
        raise ValueError("pass either in_place=True or target_path, not both")

    resolved = resolve_run(run, use_routed=use_routed)
    rep = resolved["report"]
    stage_approved = rep.get("quality") is not None
    if not stage_approved and not rep.get("passed", False) and not allow_failed_gates:
        failed_gates = [g["name"] for g in rep.get("gates", []) if not g.get("passed")]
        raise ApplyError(
            f"refusing to apply run with failed gates ({', '.join(failed_gates) or 'passed=False'}); "
            "fix the run or pass allow_failed_gates=True explicitly"
        )

    input_path: Path = resolved["input_board_path"]
    input_sha: str | None = resolved["input_board_sha256"]
    artifact_path: Path = resolved["artifact_path"]
    artifact_sha: str = resolved["artifact_sha256"]
    reviewed_sidecars = resolved["artifact_sidecars"]

    if adapter == "kicad_ipc":
        _check_kicad_ipc()
        raise ApplyError("kicad_ipc live mutation is not yet enabled; use adapter='kicad_file'")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    apply_id = f"{input_path.stem}-{stamp}-{uuid4().hex[:6]}"
    hist_root = _default_history_dir(history_dir)
    entry_dir = hist_root / apply_id

    if adapter == "altium_plan":
        if dry_run:
            src_board = read_board(input_path)
            dst_board = read_board(artifact_path)
            flipped = [fp.ref for fp in dst_board.footprints if fp.side != src_board.fp(fp.ref).side]
            if flipped and not allow_flip:
                raise ApplyError(f"altium_plan dry-run blocked {len(flipped)} side flips (assembly level 1)")
            return {
                "dry_run": True, "adapter": adapter, "run_report": str(resolved["run_report_path"]),
                "artifact_kind": resolved["artifact_kind"], "artifact_path": str(artifact_path),
                "flipped_refs": flipped,
            }
        bundle = _build_altium_plan_bundle(resolved, entry_dir, allow_flip=allow_flip)
        manifest = {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "apply_id": apply_id,
            "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "adapter": adapter,
            "in_place": False,
            "run_report": str(resolved["run_report_path"]),
            "artifact_kind": resolved["artifact_kind"],
            "artifact_path": str(artifact_path),
            "artifact_sha256": artifact_sha,
            "input_board_path": str(input_path),
            "input_board_sha256": input_sha,
            "target_path": bundle["apply_plan"],
            "before_sha256": input_sha,
            "after_sha256": sha256_file(Path(bundle["apply_plan"])),
            "altium_bundle": bundle,
            "backup_files": [],
            "created_files": [bundle["apply_plan"], bundle["revert_plan"]],
            "reverted": False,
            "elapsed_ms": round((time.monotonic() - t0) * 1000, 2),
        }
        _atomic_write_text(entry_dir / "manifest.json", json.dumps(manifest, indent=1, sort_keys=True))
        return manifest

    # adapter == "kicad_file"
    if in_place:
        target = input_path
        if not target.is_file():
            raise ApplyError(f"in_place target board does not exist: {target}")
        live_sha = sha256_file(target)
        if input_sha and live_sha != input_sha and not force_stale:
            raise ApplyError(
                f"stale target board: {target} was modified since the run "
                f"(expected sha256 {input_sha[:12]}, live {live_sha[:12]}). "
                "Re-run placement on the latest board or pass force_stale=True."
            )
    else:
        if target_path is not None:
            target = Path(target_path).resolve()
            if target == input_path:
                raise ApplyError(
                    "target_path equals the original source board (principle P8); "
                    "pass in_place=True explicitly to overwrite with transactional backup"
                )
        else:
            suffix = ".routed.applied.kicad_pcb" if resolved["artifact_kind"] == "routed" else ".applied.kicad_pcb"
            target = input_path.with_name(f"{input_path.stem}{suffix}").resolve()
        live_sha = sha256_file(target) if target.is_file() else None

    if reviewed_sidecars is not None:
        target_sidecars = {suffix: sha256_file(target.with_suffix(suffix))
                           for suffix in SIDECARS if target.with_suffix(suffix).is_file()}
        if in_place and target_sidecars != reviewed_sidecars:
            raise ApplyError("in-place target rules differ from the reviewed project rules")
        if not in_place and target_sidecars.keys() - reviewed_sidecars.keys():
            raise ApplyError("destination contains project rules outside the reviewed artifact")

    route_m = (rep.get("metrics") or {}).get("route") or {}
    unrouted_count = int(route_m.get("unrouted") or 0) if route_m else None
    dangling_count = int(route_m.get("drc_track_dangling") or 0) + int(route_m.get("drc_via_dangling") or 0)
    apply_warnings: list[str] = []
    if resolved["artifact_kind"] == "routed" and route_m:
        if unrouted_count and unrouted_count > 0:
            apply_warnings.append(
                f"Applied routed board is NOT fab-ready: {unrouted_count} unconnected item(s) remain "
                f"(coverage={route_m.get('coverage')}). Run diagnose -> fix -> re-route before fabrication."
            )
        if dangling_count > 0:
            apply_warnings.append(
                f"Applied routed board has {dangling_count} dangling track/via warning(s) in KiCad DRC."
            )

    if dry_run:
        return {
            "dry_run": True,
            "adapter": adapter,
            "in_place": in_place,
            "run_report": str(resolved["run_report_path"]),
            "artifact_kind": resolved["artifact_kind"],
            "artifact_path": str(artifact_path),
            "artifact_sha256": artifact_sha,
            "input_board_path": str(input_path),
            "input_board_sha256": input_sha,
            "target_path": str(target),
            "target_exists": target.is_file(),
            "target_live_sha256": live_sha,
            "warnings": apply_warnings,
        }

    backup_dir = entry_dir / "backup"
    backup_files: list[dict[str, str]] = []
    created_files: list[str] = []

    # Step 1: Hermetic verified backup of any pre-existing target + sidecars before any mutation.
    if target.is_file():
        b_dst = backup_dir / target.name
        b_sha = _atomic_copy(target, b_dst)
        backup_files.append({"original": str(target), "backup": str(b_dst), "sha256": b_sha})
        for suf in SIDECARS:
            sc = target.with_suffix(suf)
            if sc.is_file():
                sc_b = backup_dir / sc.name
                sc_sha = _atomic_copy(sc, sc_b)
                backup_files.append({"original": str(sc), "backup": str(sc_b), "sha256": sc_sha})

    # Keep the baked-in rule files and the board together if a later copy fails.
    published = {}
    try:
        after_sha = _atomic_copy(artifact_path, target, expected_sha256=artifact_sha)
        published[target] = after_sha
        if not backup_files:
            created_files.append(str(target))
        if not in_place:
            backed_up_paths = {row["original"] for row in backup_files}
            for suf in SIDECARS:
                src_sc = artifact_path.with_suffix(suf)
                if reviewed_sidecars is not None:
                    if suf not in reviewed_sidecars:
                        continue
                    if not src_sc.is_file():
                        raise ApplyError(f"reviewed rule file disappeared: {src_sc}")
                elif not src_sc.is_file():
                    src_sc = input_path.with_suffix(suf)
                dst_sc = target.with_suffix(suf).resolve()
                if not src_sc.is_file() or src_sc.resolve() == dst_sc:
                    continue
                existed = dst_sc.is_file()
                if existed and str(dst_sc) not in backed_up_paths:
                    sc_b = backup_dir / dst_sc.name
                    sc_sha = _atomic_copy(dst_sc, sc_b)
                    backup_files.append({"original": str(dst_sc), "backup": str(sc_b), "sha256": sc_sha})
                    backed_up_paths.add(str(dst_sc))
                published[dst_sc] = _atomic_copy(src_sc, dst_sc, expected_sha256=(reviewed_sidecars or {}).get(suf))
                if not existed:
                    created_files.append(str(dst_sc))
    except (OSError, ApplyError):
        backups = {row["original"]: row for row in backup_files}
        for written, digest in reversed(list(published.items())):
            if not written.is_file() or sha256_file(written) != digest:
                raise ApplyError(f"destination changed during apply; retained backups in {backup_dir}") from None
            previous = backups.get(str(written))
            if previous is not None:
                _atomic_copy(Path(previous["backup"]), written, expected_sha256=previous["sha256"])
            else:
                written.unlink()
        raise

    manifest = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "apply_id": apply_id,
        "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "adapter": adapter,
        "in_place": in_place,
        "run_report": str(resolved["run_report_path"]),
        "artifact_kind": resolved["artifact_kind"],
        "artifact_path": str(artifact_path),
        "artifact_sha256": artifact_sha,
        "input_board_path": str(input_path),
        "input_board_sha256": input_sha,
        "target_path": str(target),
        "before_sha256": live_sha,
        "after_sha256": after_sha,
        "backup_files": backup_files,
        "created_files": created_files,
        "warnings": apply_warnings,
        "reverted": False,
        "elapsed_ms": round((time.monotonic() - t0) * 1000, 2),
    }
    _atomic_write_text(entry_dir / "manifest.json", json.dumps(manifest, indent=1, sort_keys=True))
    return manifest


def list_applies(history_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """Return recorded apply manifests sorted newest first."""
    hist_root = _default_history_dir(history_dir)
    if not hist_root.is_dir():
        return []
    out = []
    for mf in hist_root.glob("*/manifest.json"):
        try:
            out.append(json.loads(mf.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    out.sort(key=lambda m: (m.get("created_utc", ""), m.get("apply_id", "")), reverse=True)
    return out


def revert_apply(
    apply_id: str = "latest",
    *,
    history_dir: str | Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Revert a recorded apply transaction and prove restoration via SHA-256 (< 2 s gate)."""
    t0 = time.monotonic()
    hist_root = _default_history_dir(history_dir)

    if apply_id == "latest":
        active = [m for m in list_applies(hist_root) if not m.get("reverted")]
        if not active:
            raise ApplyError(f"no active (non-reverted) apply found in {hist_root}")
        manifest = active[0]
        manifest_path = hist_root / manifest["apply_id"] / "manifest.json"
    else:
        if not apply_id or "/" in apply_id or "\\" in apply_id or ".." in apply_id:
            raise ApplyError(f"invalid apply_id: {apply_id!r}")
        entry_resolved = (hist_root / apply_id).resolve()
        if entry_resolved.parent != hist_root:
            raise ApplyError(f"invalid apply_id path outside history_dir: {apply_id!r}")
        manifest_path = entry_resolved / "manifest.json"
        if not manifest_path.is_file():
            raise ApplyError(f"apply manifest not found: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if manifest.get("reverted"):
        raise ApplyError(f"apply {manifest['apply_id']!r} was already reverted at {manifest.get('reverted_utc')}")

    target = Path(manifest["target_path"]).resolve()
    after_sha = manifest.get("after_sha256")
    if target.is_file() and after_sha and not force:
        live_sha = sha256_file(target)
        if live_sha != after_sha:
            raise ApplyError(
                f"target {target} was edited after apply (expected {after_sha[:12]}, live {live_sha[:12]}); "
                "pass force=True to overwrite post-apply changes"
            )

    restored: list[dict[str, str]] = []
    removed: list[str] = []

    if manifest["adapter"] == "altium_plan":
        elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
        manifest["reverted"] = True
        manifest["reverted_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
        manifest["revert_elapsed_ms"] = elapsed_ms
        _atomic_write_text(manifest_path, json.dumps(manifest, indent=1, sort_keys=True))
        return {
            "reverted": True,
            "apply_id": manifest["apply_id"],
            "adapter": manifest["adapter"],
            "revert_plan": manifest["altium_bundle"]["revert_plan"],
            "elapsed_ms": elapsed_ms,
            "within_budget": elapsed_ms <= REVERT_BUDGET_S * 1000,
        }

    # Restore every backed-up file atomically with SHA-256 verification.
    for row in manifest.get("backup_files") or ():
        orig = Path(row["original"]).resolve()
        bak = Path(row["backup"]).resolve()
        if not bak.is_file():
            raise ApplyError(f"backup file missing for revert: {bak}")
        got = _atomic_copy(bak, orig, expected_sha256=row["sha256"])
        restored.append({"path": str(orig), "sha256": got})

    # Remove files newly created by a non-in-place apply.
    for created_raw in manifest.get("created_files") or ():
        created = Path(created_raw).resolve()
        if created.is_file():
            created.unlink()
            removed.append(str(created))

    # Verify final state matches pre-apply state (or input board untouched for copy apply).
    verified_sha = sha256_file(target) if target.is_file() else None
    if manifest.get("before_sha256") is not None and verified_sha != manifest["before_sha256"]:
        raise ApplyError(
            f"post-revert SHA-256 mismatch on {target}: expected {manifest['before_sha256']}, got {verified_sha}"
        )
    input_path = Path(manifest["input_board_path"]).resolve()
    input_untouched = (
        input_path.is_file() and manifest.get("input_board_sha256") == sha256_file(input_path)
    ) if manifest.get("input_board_sha256") else None

    elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
    manifest["reverted"] = True
    manifest["reverted_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
    manifest["revert_elapsed_ms"] = elapsed_ms
    _atomic_write_text(manifest_path, json.dumps(manifest, indent=1, sort_keys=True))
    return {
        "reverted": True,
        "apply_id": manifest["apply_id"],
        "adapter": manifest["adapter"],
        "in_place": manifest["in_place"],
        "target_path": str(target),
        "restored_sha256": verified_sha,
        "input_untouched": input_untouched,
        "restored_files": restored,
        "removed_files": removed,
        "elapsed_ms": elapsed_ms,
        "within_budget": elapsed_ms <= REVERT_BUDGET_S * 1000,
    }


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="placer-apply", description="Safe reversible EDA apply & revert (F7)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_app = sub.add_parser("apply", help="Apply a verified placement/routing run")
    p_app.add_argument("run", help="Path to run directory or run_report.json")
    p_app.add_argument("--adapter", choices=ADAPTERS, default="kicad_file")
    p_app.add_argument("--target", default=None, help="Output file path (non-in-place)")
    p_app.add_argument("--in-place", action="store_true", help="Overwrite original board with atomic backup")
    p_app.add_argument("--placed-only", action="store_true", help="Apply placed board even if routed board exists")
    p_app.add_argument("--allow-failed-gates", action="store_true")
    p_app.add_argument("--force-stale", action="store_true")
    p_app.add_argument("--allow-flip", action="store_true")
    p_app.add_argument("--dry-run", action="store_true")
    p_app.add_argument("--history-dir", default=None)

    p_rev = sub.add_parser("revert", help="Revert a previous apply transaction (< 2s)")
    p_rev.add_argument("apply_id", nargs="?", default="latest")
    p_rev.add_argument("--force", action="store_true")
    p_rev.add_argument("--history-dir", default=None)

    p_ls = sub.add_parser("list", help="List recorded apply transactions")
    p_ls.add_argument("--history-dir", default=None)

    args = parser.parse_args(argv)
    if args.cmd == "apply":
        res = apply_run(
            args.run, history_dir=args.history_dir, adapter=args.adapter, target_path=args.target,
            in_place=args.in_place, use_routed=not args.placed_only,
            allow_failed_gates=args.allow_failed_gates, force_stale=args.force_stale,
            allow_flip=args.allow_flip, dry_run=args.dry_run,
        )
    elif args.cmd == "revert":
        res = revert_apply(args.apply_id, history_dir=args.history_dir, force=args.force)
    else:
        res = {"applies": list_applies(args.history_dir)}
    print(json.dumps(res, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
