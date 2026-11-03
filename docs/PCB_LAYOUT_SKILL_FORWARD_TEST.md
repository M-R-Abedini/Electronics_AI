# PCB layout Skill forward-test

Date: 2026-11-02. Checkout declares plugin `0.6.0` and native script
`2026.11.02.placement1`.

## Scope of this evidence

This was an agent walkthrough of the new Skill with actual execution of
`LayoutService.call_tool_async`, the service behind the registered `layout__`
tools. It used a new six-component synthetic mixed-domain case, not ADS1262,
Pt100, U12 or a copy of a production board. No successful tool result was mocked.
A native-dispatch tripwire recorded zero calls. State/journal artifacts were
written only in a private test directory.

The evaluator authored the Skill but did not author the placement core/native
implementation. This is therefore independent of that implementation, but is
**not a blinded second-author Skill evaluation or expert layout review**. It
does not test MCP JSON-RPC transport, live Altium, native readback, raw/nested
gateway enforcement or manufacturing readiness. Those need their separate
integration/acceptance evidence.

## Exact task given to the Skill

> Use the bundled electronics-ai-pcb-layout Skill to review the supplied six-part
> mixed-domain PCB and propose a scoped placement improvement. Preserve the existing
> board outline and compare the baseline with alternatives. The board contains a
> declared sensitive-input block and a declared switching power block.
> Improve interconnect compactness where justified and explain the engineering
> trade-offs and verification needed. No exact manufacturer parts have been selected.
> Use the available layout APIs with the supplied raw geometry and intent. Record
> what you can complete and any dependent inputs or checks you still need.

The raw case has a 40 x 30mm rectangular outline. Origins, rotations and all
pad/courtyard geometry are synthetic test data in millimetres. A 0.2mm minimum
pad clearance is explicitly a fixture `engineering_policy`, not a manufacturer
or universal design limit. All six components start on top; no bottom assembly
authorization is inferred.

| ID / reference | Origin (mm) | Declared functional scope |
|---|---|---|
| a1 / A1 | 10, 15 | Sensitive input block |
| c1 / C1 | 12.5, 15 | Sensitive input block |
| j1 / J1 | 5, 15 | Sensitive input block |
| u2 / U2 | 24, 15 | Switching power block |
| c2 / C2 | 21.5, 15 | Switching power block |
| l2 / L2 | 27, 15 | Switching power block |

The declared input path is `j1.sig -> a1.in`. Analog return architecture,
switching states, commutation pin loops and actual applicable placement-phase
constraint IDs were not supplied. No selected manufacturer/package data or
physical copper was supplied. Functional declarations are input-contract
evidence, not authenticated datasheet facts.

## Observed tool sequence

The qualified names below identify the actual exported contracts. Execution
called their service handler with the unprefixed method name, not an MCP client.

| Call | Observed result |
|---|---|
| `layout__get_contracts({})` | Returned policy version, nested schemas and native apply limitations. |
| `layout__capture_context({board_context: ...})` | Registered the synthetic context; `suitable_as_native_precondition=false`. A caller-supplied `document_modified=false` did not create live authority. |
| `layout__get_workflow({context_id})` | Existing placement-review state machine suggested stage `placement`, with the fresh evidence/readback exit gate. It did not request schematic reconstruction. |
| `layout__plan_placement({context_id, design_intent, constraints: [], options})` | Compared baseline and `refine:29`/`refine:30`; `selection_status=blocked`, `selected_candidate_id=baseline`, `native_edits=false`. |
| `layout__validate_plan({plan_id})` | `passed=false`; unresolved profile checks and `trusted_live_capture` remained blockers. |
| `layout__get_evidence({evidence_id,kind: "validations"})` | Retrieved the stored failed validation intact; this did not become a freshness or success assertion. |
| Negative admission probe: `layout__apply_plan({plan_id,validation_id,operation_id})` | Raised `ValueError`: `A passing server-generated validation for this exact plan is required`. Native-dispatch calls remained zero. |

The final call was an intentional **offline rejection probe**, not the Skill's
recommended action after a failed gate. Following the Skill stops mutation at
the failed validation; it does not try legacy/raw tools or end the managed scope
to manufacture a successful qualification.

The returned profiles were `precision_analog` for a1/c1/j1 and `switching_power`
for c2/l2/u2. No digital-high-speed, isolation or part-number-specific placement
template was introduced. Missing facts were retained as scoped obligations.

| Candidate | Pad HPWL proxy (mm) | Input chord lower bound (mm) | Result |
|---|---:|---:|---|
| baseline | 37.25 | 4.5 | Infeasible/unknown required evidence |
| refine:29 | 20.262 | 2.151 | Infeasible/unknown required evidence |
| refine:30 | 20.262 | 2.151 | Infeasible/unknown required evidence |

Both alternatives improved those two geometry proxies. That did not compensate
for unknown return/commutation evidence. Selecting `baseline` while status is
`blocked` means **an unqualified reference is retained**, not that baseline has
been certified good. No route length, loop inductance, real return path, DRC or
manufacturing pass was inferred from the shorter chord or HPWL.

The recorded blocker set was:

```text
candidate_selection_blocked
profile:power:switching_power:commutation_pin_loops
profile:power:switching_power:return_intent
profile:power:switching_power:scoped_checks
profile:power:switching_power:switching_states
profile:sensitive:precision_analog:return_intent
profile:sensitive:precision_analog:scoped_checks
trusted_live_capture
```

Minimal dependent follow-up inputs are the actual return/reference architecture,
switching-state and commutation pin bindings, relevant sourced constraints and a
trusted live capture before native application. Independent offline geometry
review can continue. Repeated permission prompts are not a substitute for these
missing engineering inputs.

## Reproduce the raw case and service calls

Run this Python code with the checkout's environment and repository root on
`sys.path`. `eda-agent/src` is added explicitly so a separately installed older
wheel cannot silently supply a different core. The generated test state stays
inside `TemporaryDirectory`. Its factory values describe a synthetic fixture.

```python
import asyncio, tempfile, sys
from pathlib import Path
sys.path.insert(0, str(Path("eda-agent/src").resolve()))
from suite_layout import LayoutService

source = {"id": "forward-fixture-v1", "kind": "engineering_policy"}
board = {"schema": 1, "units": "mm", "source": "synthetic",
    "snapshot_id": "forward-fixture-v1", "board_path": "D:/synthetic/forward-board.PcbDoc",
    "document_modified": False, "origin_mm": [0, 0],
    "outline": [[0,0],[40,0],[40,30],[0,30]], "components": [], "pads": [],
    "fabrication_profile": {"minimum_pad_clearance_mm": .2, "source": source},
    "assembly_profile": {"allowed_sides": ["top"], "source": source},
    "coverage": {"outline": "complete", "routing_obstacles": "unknown"},
    "layers": [{"name":"Top","kind":"signal","order":0},
               {"name":"Bottom","kind":"signal","order":1}]}
for cid, ref, x in [("a1","A1",10),("c1","C1",12.5),("j1","J1",5),
                    ("u2","U2",24),("c2","C2",21.5),("l2","L2",27)]:
    board["components"].append({"id":cid,"reference":ref,"x_mm":x,"y_mm":15,
        "rotation_deg":0,"side":"top","fixed":False,"locked":False,
        "allowed_rotations_deg":[0,90,180,270],"allowed_sides":["top"],
        "courtyard_local":[[-.8,-.8],[.8,-.8],[.8,.8],[-.8,.8]],"courtyard_source":source})
for cid, pad, net, x, y in [
    ("a1","in","INPUT",-.5,0),("a1","vdd","VDD",.5,.5),("a1","gnd","AGND",.5,-.5),
    ("c1","p","VDD",-.25,0),("c1","n","AGND",.25,0),
    ("j1","sig","INPUT",0,0),("j1","ret","AGND",.5,0),
    ("u2","vin","VIN",-.5,.5),("u2","sw","SW",.5,.5),("u2","gnd","PGND",-.5,-.5),
    ("c2","p","VIN",-.25,0),("c2","n","PGND",.25,0),
    ("l2","in","SW",-.4,0),("l2","out","VDD",.4,0)]:
    board["pads"].append({"id":cid+"."+pad,"component_id":cid,"pad_name":pad,
        "net_name":net,"local_xy_mm":[x,y],"width_mm":.2,"height_mm":.2,
        "shape":"rectangle","rotation_deg":0,"layer":"Top"})
intent = {"blocks":[
    {"id":"sensitive","component_ids":["a1","c1","j1"],"profiles":["precision_analog"],
     "source":{"id":"declared-functional-intent","kind":"user_intent"},
     "requirements":{"sensitive_pin_bindings":{"input":["a1.in","j1.sig"]}}},
    {"id":"power","component_ids":["u2","c2","l2"],"profiles":["switching_power"],
     "source":{"id":"declared-functional-intent","kind":"user_intent"},"requirements":{}}],
    "critical_paths":[{"id":"input_path","pad_ids":["j1.sig","a1.in"]}]}

async def run(directory):
    attempted = []
    async def native(name, args):
        attempted.append(name)
        raise AssertionError("Offline case attempted a native call")
    service = LayoutService(Path(directory), native_dispatch=native)
    async def call(name, args):
        return await service.call_tool_async(name, args)
    await call("get_contracts", {})
    context = await call("capture_context", {"board_context":board})
    await call("get_workflow", {"context_id":context["context_id"]})
    plan = await call("plan_placement", {"context_id":context["context_id"],
        "design_intent":intent,"constraints":[],
        "options":{"seed":29,"candidate_count":2,"iterations":8,"mode":"refine"}})
    gate = await call("validate_plan", {"plan_id":plan["plan_id"]})
    evidence = await call("get_evidence", {"evidence_id":gate["validation_id"],"kind":"validations"})
    print(plan["selection_status"], gate["passed"], gate["unresolved_blockers"])
    try:
        await call("apply_plan", {"plan_id":plan["plan_id"],
            "validation_id":gate["validation_id"],"operation_id":"forward-synthetic-01"})
    except ValueError as error:
        print(type(error).__name__, str(error))
    assert not attempted
with tempfile.TemporaryDirectory(prefix="pcb-skill-forward-") as temporary:
    asyncio.run(run(temporary))
```

The captured run's input artifact SHA256 was
`eda33c1a255f23fd1ced0f1e914bfeaf26345234350bc4b5afe08fffd41ed3ce`.
That artifact includes the exact task text as well as raw context/intent/options;
its formatting is separate from the service's canonical evidence digests.
Validation IDs include capture/measurement state and need not match a later run.

## Acceptance boundary

This exercise demonstrates conditional profile use, visible unknown project
inputs, rejection despite attractive geometry proxies, and service-level refusal
of an unqualified apply. It does not demonstrate physical switching/return
verification, expert calibration, a routed board or native recovery. Trial
routing, surface/3D creepage and held-out expert-layout benchmarking remain
separate work; they cannot be closed by adding text to the Skill.
