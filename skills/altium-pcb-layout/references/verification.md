# Verification and recovery

Use the current gateway and native adapter contract, not a workflow copied from
another MCP. The normal mutation sequence is capture, exact plan/preview,
validation, suitable checkpoint, apply that plan, readback, then affected checks.
Discover required identifiers and recovery fields from the real schema.

- A saved-file hash cannot certify a live unsaved buffer. A supported live
  precondition must bind the explicit document and the geometry/constraints
  being reviewed. Refuse a stale preview, changed move list or target mismatch.
- A successful tool envelope is not necessarily a successful batch. Retain
  per-object applied/failed/unverified results, readback and checkpoint IDs.
  A partial or ambiguous write goes to inspection/recovery, not blind retry.
- Existing automatic checkpoints preserve saved disk bytes. They do not
  capture every unsaved Altium object, force a save, or guarantee native Undo
  and transactional rollback. Do not claim recovery beyond the receipt.
- Restore currently requires Altium to be closed. Respect that condition;
  neither a quiet queue nor a timeout authorizes restoring an open document or
  terminating the user's application.
- A `design_session_log` event is a journal statement. It is not independent
  check evidence. Require the implemented server gate and fresh evidence for
  the scope before mutation; do not use a raw or nested alias to bypass it.
- Gateway safeguards do not govern an independently connected standalone
  worker or a manual external edit. State that boundary and revalidate after
  external changes. Scope/effect, not a caller's claimed purpose, governs an
  operation's obligations.

Reuse the existing retry limit. A retry needs a changed hypothesis or corrective
action. Unsupported coverage, stale evidence and missing inputs are not solver
failures that merit repeated optimization. Report the dependent blocker and
continue an independent authorized scope when possible.

Keep visual and electrical coverage separate. `eda__pcb_render_svg` currently
omits region/pour and full component-body outline coverage. It can help inspect
supported labels, orientation and spacing, but cannot prove absence of a plane,
return path, short or clearance violation. A fresh native DRC additionally needs
its actual report scope and exceptions; cached violations are a separate result.

For manufacturing release report connectivity, unrouted exceptions, DRC, stackup
and fabrication/assembly checks independently. Simulation or hardware testing
is a further evidence level. A placement gate does not require every ordinary
net to be routed, and does not certify a manufacturing-ready board.
