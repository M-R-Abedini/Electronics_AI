# Upstream sync policy

Electronics AI vendors a snapshot of
[`salitronic/eda-agent`](https://github.com/salitronic/eda-agent)
(Apache-2.0) under `eda-agent/`. The exact snapshot commit is pinned in
`UPSTREAM.json`; every file changed relative to that snapshot is listed
in `MODIFICATIONS.md`. Upstream is actively developed, so the fork
drifts unless syncing is a scheduled habit rather than an accident.

## Cadence

Once a month, or immediately when upstream ships a bridge/reliability
fix, run through this checklist on a dedicated `sync/upstream-YYYY-MM`
branch:

1. `git fetch https://github.com/salitronic/eda-agent.git` in a scratch
   clone and note the new upstream HEAD.
2. Diff the pinned snapshot commit (`UPSTREAM.json` →
   `eda-agent.snapshot_commit`) against that HEAD, limited to paths
   this fork ships. Prioritise, in order:
   - DelphiScript bridge crash/hang fixes (`scripts/altium/*.pas`)
   - Transport, recovery and process-lifecycle fixes (`bridge/`, `core/`)
   - Tool correctness fixes in modules the suite exposes
   - Anything else is opt-in, not automatic.
3. Apply upstream changes file-by-file. Where a file also carries suite
   modifications (see `MODIFICATIONS.md`), merge manually; never
   overwrite a suite change silently. If a conflict cannot be resolved
   confidently, keep the local version and record the skipped upstream
   commit in `MODIFICATIONS.md` with a one-line reason.
4. After the Pascal sources change in any way, bump `SCRIPT_VERSION`
   in `eda-agent/scripts/altium/Main.pas` (the repo convention Python
   relies on to detect stale compiled scripts) and rebuild the bundle
   with `python eda-agent/scripts/altium/build.py`.
5. Run the full gates before merging the sync branch:
   - `python -m pytest tests -q` (repository root)
   - `python -m pytest tests -q` in `eda-agent/`
   - `python tests/smoke_stdio.py`
6. Update `UPSTREAM.json` (`snapshot_commit`) and `MODIFICATIONS.md`
   in the same PR, so provenance always matches the shipped tree.

## What syncing must never do

- Never replace the suite gateway, native library codecs, placement
  evidence stack, or SI/PI services with upstream equivalents without
  an explicit product decision; they are this repository's reason to
  exist.
- Never drop the license/notice files (`eda-agent/LICENSE`,
  `eda-agent/NOTICE`, `THIRD_PARTY_NOTICES.md`).
- Never merge a sync with red CI. The suites are the only proof that
  upstream behaviour and suite modifications still compose.

The `coffeenmusic/altium-mcp` snapshot tracked in `UPSTREAM.json`
covers only the ANSI text validation in `suite_json_guard.py`; it has
no recurring sync.
