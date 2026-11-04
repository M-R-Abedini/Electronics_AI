# Windows PCB acceptance — Electronics AI

`scripts/windows_pcb_acceptance.py` verifies the selected **installed** plugin over
the real MCP stdio protocol. It uses `scripts/plugin_runtime.py --data-dir ...
--plugin-root ... serve`, which resolves the configured private Python runtime.
No GUI automation, source-checkout fallback or obsolete bridge is used.

The default run checks MCP identity/version, capability discovery and
`eda__app_context`: an actual native reply, matching packaged/compiled script
versions, and matching content-addressed script project identity. Keep exactly
one intended Altium instance open. Native startup can run the packaged polling
script; the helper does not open, create, save or edit a design.

Run using the Python 3.12 interpreter from the current plugin data directory:

```powershell
$plugin = "$env:USERPROFILE\.codex\plugins\cache\electronics-ai\mcp-electronics-ai\1.0.0"
$data = "$env:USERPROFILE\.codex\plugins\data\mcp-electronics-ai"
$python = (Get-Content -Raw -LiteralPath "$data\state.json" | ConvertFrom-Json).python
$report = Join-Path $env:TEMP ("electronics-ai-acceptance-" + [guid]::NewGuid() + ".json")
& $python "$plugin\scripts\windows_pcb_acceptance.py" --plugin-root $plugin --data-dir $data --output $report
```

If your installed cache or data directory differs, substitute the actual
absolute paths. The helper requires explicit paths and never overwrites an
existing report. A missing dependency means the selected interpreter is not
the configured private runtime; do not install packages as a test side effect.

## Optional real PCB capture

Open an existing real `.PcbDoc` in that same Altium instance yourself, then add
`--board-path 'D:\absolute\path\ExistingBoard.PcbDoc'` to the command. The helper
never opens the file. A file on disk which is not already loaded must fail the
native target check.

The additional checks are read-only:

1. `eda__pcb_get_components(include_package_geometry=True)` returns component
   pads in millimetres and the requested target identity. No mechanical layer
   is guessed to be a courtyard. Raw primitives do not qualify a closed boundary.
2. `layout__capture_context` creates private evidence, and
   `layout__get_evidence` reads back its immutable context handle, actual native
   provenance, board path and source dependency digest. Missing courtyards,
   cutout coverage or fabrication data remain unknown; capture is not approval
   for placement.
3. A separate nonexistent, unloaded absolute PCB path is sent to the component
   reader. Only an explicit `ERR_TARGET_DOCUMENT_MISMATCH` error passes this
   check; a successful fallback or an unrelated error fails it.
4. The saved PCB SHA256 is compared before and after these reads. This does not
   certify the in-memory buffer: the native modified flag has incomplete
   detection coverage. Other applications or a person can also change the file
   during the test, causing the byte check to fail.

The report can contain component names, net information, paths and geometry.
Keep it private unless those design details are intended for publication.

## What a passing report means

Exit `0` / `status: PASS` means the checks selected for this **read-only** run
passed. With no board selected, PCB geometry acceptance is `DEFERRED`.
`fixture_U12: not_present_in_repo` is explicit: this helper invents no U12,
ADS1262, transducer layout or synthetic production board. Native placement apply
acceptance remains `DEFERRED`: no qualified stored placement plan was supplied
or applied. No lock, stale-state or once-only mutation test is claimed as
executed by this helper.

## Separate native placement acceptance checklist

Perform these checks only on an explicitly identified disposable copy of an
existing real PCB, with a qualified stored plan and the needed project data.
They are not commands executed by this helper. Never treat an offline example
or a caller's successful-stage claim as a native precondition.

- [ ] Record the selected installed package/native versions and explicit loaded
      board identity. Preserve the saved baseline, actual geometry, locks,
      participating rules and dirty-state limitations. Unknown safety inputs
      or unqualified courtyard/cutout coverage must block qualification.
- [ ] Capture the context and read the server-created evidence. Record its
      context ID, board revision/capture digest and policy version. Supply
      design intent, project-specific fabrication constraints and qualified
      package geometry; do not copy numerical limits from a different project.
- [ ] Plan and validate through the actual `layout__` tools. Read back the
      stored plan/validation. Record the exact canonical moves, affected object
      identities, digests and baseline comparison. Any individual critical
      VCC/GND leg regression must fail. Visual alignment alone cannot replace
      the baseline. A failed or unknown gate cannot be changed by logging `ok`.
- [ ] On the disposable copy, check that stale context, changed unselected
      geometry/rules/locks, a different target, or altered plan/validation IDs
      refuse before mutation. A locked target must be refused in preflight.
      Record actual receipts/refusals; do not infer them from unit tests.
- [ ] Apply only the exact validated stored plan with a fresh operation ID.
      Confirm the disk checkpoint and original-pose evidence precede the
      native write. The optimizer must not rerun during apply. There is no
      promised transactional rollback and no automatic save.
- [ ] Record the actual native receipt, applied/skipped/failed/unverified
      objects, final coordinate/rotation/side readback and whole-state
      comparison. Partial or unverified results remain unqualified.
- [ ] Repeat the same operation identity only to inspect the once-only receipt
      behavior: no second write may occur. A reused operation ID with different
      moves/target must be rejected. If execution times out or ownership is
      uncertain, inspect Altium and existing evidence; never blindly replay.
- [ ] Recheck live freshness and the placement/verification workflow gates.
      Preserve baseline versus actual post-state evidence. Routing completion,
      DRC and fabrication release require their own checks and are not proven
      by a successful placement receipt.

Leave each unexecuted item `DEFERRED` with its missing prerequisite. A green
read-only health report must not be presented as completion of this checklist.
