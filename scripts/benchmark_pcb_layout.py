"""Reproducible synthetic PCB decision benchmarks; never opens or edits CAD.

Run from the repository with its Python environment. These are counterexamples
and held-out computational seeds, not expert layouts or routing benchmarks.
SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import subprocess
import sys
import time
from collections import Counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eda-agent" / "src"))

from eda_agent.design.placement_evidence import (  # noqa: E402
    POLICY_VERSION, compare_placement_candidates, digest, example_board_context,
    generate_placement_candidates,
)

SOURCE = {"kind": "engineering_policy", "id": "synthetic-benchmark-only", "revision": "1"}
HELD_OUT_SEEDS = (211, 307, 401)
DEVELOPMENT_SEEDS = (0, 1, 7, 8)


def move(component, **pose):
    return {"component_id": component, **pose}


def pad(identifier, component, net, point=(0, 0), *, layer="Top", size=.2):
    return {"id": identifier, "component_id": component, "net_name": net,
            "local_xy_mm": list(point), "width_mm": size, "height_mm": size,
            "shape": "rectangle", "rotation_deg": 0, "layer": layer}


def profile(identifier, components, kind, check_ids):
    common = {"placement_check_ids": list(check_ids)}
    if kind == "precision_analog":
        common.update(sensitive_pin_bindings={"input": "Acritical", "output": "Bcritical"},
                      return_intent={"reference_net": "RETURN", "status": "declared; not routed"})
    elif kind == "switching_power":
        common.update(switching_states=[{"id": "state_A", "pin_path": ["a.1", "b.1"]},
                                        {"id": "state_B", "pin_path": ["b.1", "a.1"]}],
                      commutation_pin_loops=[["a.1", "b.1", "a.1"]],
                      return_intent={"reference_net": "RETURN", "status": "project intent only"})
    elif kind == "digital_highspeed":
        common.update(interface_pin_bindings={"driver": "a.1", "receiver": "b.1"},
                      reference_intent={"layer": "Top", "net": "RETURN"},
                      timing_requirements={"placement_chord_limit_mm": 20,
                          "basis": "synthetic geometry budget; delay/skew remain unverified"})
    return {"id": identifier, "component_ids": list(components), "profiles": [kind],
            "source": SOURCE, "requirements": common}


def distance_constraint(identifier, pair, limit, components, *, relation="max"):
    return {"id": identifier, "check": "pad_distance", "kind": "hard", "relation": relation,
            "measure": "center", "scope": {"components": list(components), "pads": list(pair)},
            "limit_mm": limit, "source": SOURCE}


def precision_analog(seed):
    context = example_board_context()
    rng = random.Random(seed)
    b_x = 20 + rng.uniform(-.04, .04)
    context["components"][1].update(x_mm=b_x, courtyard_local=[[-1,-1.5],[1,-1.5],[1,1.5],[-1,1.5]])
    other = copy.deepcopy(context["components"][1])
    other.update(id="c", reference="BUS_ENDPOINT", x_mm=28)
    context["components"].append(other)
    context["pads"] = [pad("Acritical", "a", "SENSITIVE"), pad("Bcritical", "b", "SENSITIVE")]
    for i in range(6):
        for cid in ("b", "c"):
            context["pads"].append(pad(cid + str(i), cid, "BUS" + str(i), (0, .25+i*.2), size=.05))
    constraints = [distance_constraint("sensitive_chord", ["Acritical", "Bcritical"], 12, ["a", "b"])]
    intent = {"blocks": [profile("front_end", ["a", "b"], "precision_analog", ["sensitive_chord"])],
              "critical_paths": [{"id": "input_path", "pad_ids": ["Acritical", "Bcritical"]}]}
    proposals = [{"id": "shorter_bus_worse_input", "moves": [move("b", x_mm=24)]}]
    return context, intent, constraints, proposals, "critical_tradeoff"


def switching_power(seed):
    context = example_board_context()
    context["components"][1]["x_mm"] += random.Random(seed).uniform(-.04, .04)
    constraints = [distance_constraint("input_connection", ["a.1", "b.1"], 12, ["a", "b"])]
    intent = {"blocks": [profile("commutation", ["a", "b"], "switching_power", ["input_connection"])],
              "critical_paths": [{"id": "commutation_chords", "pad_ids": ["a.1", "b.1", "a.1"]}]}
    proposals = [{"id": "compact_but_collision", "moves": [move("b", x_mm=14.5)]}]
    return context, intent, constraints, proposals, "hard_fail"


def digital_interface(seed):
    context = example_board_context()
    context["origin_mm"] = [10.03, 20.01]
    context["components"][1]["side"] = "bottom"
    context["pads"][1]["layer"] = "Bottom"
    context["components"][0]["rotation_deg"] = random.Random(seed).choice([0, 90, 180, 270])
    constraints = [distance_constraint("interface_placement_budget", ["a.1", "b.1"], 20, ["a", "b"])]
    intent = {"blocks": [profile("interface", ["a", "b"], "digital_highspeed", ["interface_placement_budget"])],
              "critical_paths": [{"id": "interface_chord", "pad_ids": ["a.1", "b.1"]}]}
    # A no-op is a real candidate. Baseline should win rather than creating an
    # apply operation just because the candidate came from an optimizer.
    proposals = [{"id": "unnecessary_reapply", "moves": [move("b", x_mm=24)]}]
    return context, intent, constraints, proposals, "baseline_win"


def dense_escape(seed):
    rng = random.Random(seed)
    context = example_board_context()
    context.update(origin_mm=[0,0], outline=[[0,0],[60,0],[60,50],[54,50],[54,42],[0,42]],
                   cutouts=[[[55,10],[57,10],[57,12],[55,12]]], components=[], pads=[])
    for i in range(10):
        x = 8 + (i % 5)*10 + rng.uniform(-.02, .02)
        y = 12 if i < 5 else 37
        cid = "c"+str(i)
        context["components"].append({"id":cid, "reference":"P"+str(i), "x_mm":x, "y_mm":y,
            "rotation_deg":0, "side":"top", "fixed":False,
            "courtyard_local":[[-2.25,-1.5],[2.25,-1.5],[2.25,1.5],[-2.25,1.5]], "courtyard_source":SOURCE})
        for j in range(20):
            net = "SCOPED_LINK" if i in (1,6) and j == 0 else f"N_{i}_{j}"
            context["pads"].append(pad(f"{cid}.{j}", cid, net, (-1.8+(j % 5)*.9, -.9+(j // 5)*.6)))
    reservation = {"id":"escape_bus", "layer":"Top", "start_mm":[8,25], "end_mm":[52,25],
        "width_mm":3.2, "track_width_mm":.2, "spacing_mm":.2, "margin_mm":.2,
        "net_names":["RESERVED_BUS"+str(i) for i in range(6)], "source":SOURCE}
    intent = {"reservations":[reservation]}
    proposals = [{"id":"shorter_link_blocks_channel", "moves":[move("c1", y_mm=25)]},
                 {"id":"inside_bbox_but_crosses_cutout", "moves":[move("c1", x_mm=56, y_mm=11)]}]
    return context, intent, [], proposals, "escape_fail"


def mixed_isolated(seed):
    context = example_board_context()
    for cid, x in (("c",14), ("d",24)):
        part = copy.deepcopy(context["components"][0])
        part.update(id=cid, reference="ECO_"+cid, x_mm=x+random.Random(seed+ord(cid)).uniform(-.02,.02), y_mm=35)
        context["components"].append(part)
        context["pads"].append(pad(cid+".1", cid, "INDEPENDENT"))
    analog = profile("isolated_front_end", ["a","b"], "precision_analog", ["input_geometry"])
    analog["profiles"].append("isolation")
    analog["requirements"].update(isolation_requirements=None,
        barriers=[{"layer":"Top", "region":{"outline":[[19,20],[21,20],[21,40],[19,40]]},
                   "allowed_component_ids":["b"], "copper_crossing_qualified":False}])
    constraints = [distance_constraint("input_geometry", ["a.1","b.1"], 20, ["a","b"]),
        {"id":"surface_creepage", "check":"creepage_surface", "kind":"hard", "scope":{"components":["a","b"]},
         "source":SOURCE, "limit_mm":None}]
    return context, {"blocks":[analog]}, constraints, [{"id":"move_sensitive", "moves":[move("a",x_mm=15)]}], "unknown_safety"


FAMILIES = {"precision_analog":precision_analog, "switching_power":switching_power,
            "digital_interface":digital_interface, "dense_escape":dense_escape, "mixed_isolated":mixed_isolated}


def summary(comparison):
    evaluations = []
    for report in comparison["evaluations"]:
        counts = Counter(c["status"] for c in report["checks"])
        evaluations.append({"candidate_id":report["candidate_id"], "feasible":report["feasible"],
            "status":report["status"], "check_count":len(report["checks"]), "check_statuses":dict(counts),
            "hard_fail_ids":[c["id"] for c in report["checks"] if c["required"] and c["status"] == "fail"],
            "unknown_ids":[c["id"] for c in report["checks"] if c["required"] and c["status"] == "unknown"],
            "metrics":report["metrics"], "evidence_id":report["evidence_id"]})
    encoded = json.dumps(comparison, sort_keys=True, separators=(",",":"), allow_nan=False).encode()
    return {"selection_status":comparison["selection_status"], "selected_candidate_id":comparison["selected_candidate_id"],
            "apply_eligible":comparison["plan"]["apply_eligible"], "plan_digest":comparison["plan"]["plan_digest"],
            "full_artifact_bytes":len(encoded), "evaluations":evaluations,
            "critical_regressions":{t["candidate_id"]:t["critical_regressions"] for t in comparison["tradeoffs"]}}


def compare(context, intent, constraints, candidates, artifact_dir=None, name="decision"):
    started = time.perf_counter_ns()
    result = compare_placement_candidates(context, intent, constraints, candidates)
    elapsed = (time.perf_counter_ns()-started)/1e6
    condensed = summary(result); condensed["elapsed_ms"] = round(elapsed, 3)
    if artifact_dir:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir/(name+".json")).write_text(json.dumps(result,sort_keys=True,indent=2,allow_nan=False)+"\n", encoding="utf-8")
    return result, condensed


def run_case(family, seed, iterations, artifact_dir):
    context, intent, constraints, proposals, expected = FAMILIES[family](seed)
    context["snapshot_id"] = f"synthetic:{family}:{seed}"
    case_name = f"{family}-{seed}"
    fixture_hash = digest({"context":context, "intent":intent, "constraints":constraints, "proposals":proposals})
    decision, measured = compare(context, intent, constraints, proposals, artifact_dir, case_name)
    if expected == "unknown_safety":
        assert decision["selection_status"] == "blocked" and not decision["plan"]["apply_eligible"]
    else:
        assert decision["selected_candidate_id"] == "baseline", (family, measured)
        assert decision["evaluations"][0]["feasible"], (family, measured)
    if expected == "critical_tradeoff":
        a,b = decision["evaluations"]
        assert b["feasible"] and b["metrics"]["signal_hpwl"]["value"] < a["metrics"]["signal_hpwl"]["value"]
        assert decision["tradeoffs"][1]["critical_regressions"]
    if expected == "hard_fail":
        assert measured["evaluations"][1]["hard_fail_ids"]
    if expected == "escape_fail":
        a,b = decision["evaluations"][:2]
        assert b["metrics"]["signal_hpwl"]["value"] < a["metrics"]["signal_hpwl"]["value"]
        assert "channel:escape_bus" in measured["evaluations"][1]["hard_fail_ids"]
        assert measured["evaluations"][2]["hard_fail_ids"]
    extra = {}
    if family == "dense_escape":
        scoped = copy.deepcopy(context); scoped["scope_component_ids"] = ["c1"]
        _, extra["scoped_eco"] = compare(scoped, intent, constraints,
            [{"id":"scoped_improvement", "moves":[move("c1",y_mm=14)]}], artifact_dir, case_name+"-eco")
        assert extra["scoped_eco"]["selected_candidate_id"] == "scoped_improvement"
        unknown = copy.deepcopy(context); del unknown["components"][0]["courtyard_local"]
        _, extra["unknown_package_geometry"] = compare(unknown, intent, constraints, [], artifact_dir, case_name+"-unknown")
        assert extra["unknown_package_geometry"]["selection_status"] == "blocked"
    if family == "mixed_isolated":
        scoped = copy.deepcopy(context); scoped["scope_component_ids"] = ["c"]
        _, extra["independent_eco"] = compare(scoped, intent, constraints,
            [{"id":"independent_eco", "moves":[move("c",x_mm=18)]}], artifact_dir, case_name+"-eco")
        assert extra["independent_eco"]["selected_candidate_id"] == "independent_eco"
    start = time.perf_counter_ns()
    generated = generate_placement_candidates(context, intent,
        {"mode":"refine", "seed":seed, "candidate_count":1, "iterations":iterations, "restart_radius_mm":.3})
    generator_ms = (time.perf_counter_ns()-start)/1e6
    generated_report, generated_summary = compare(context, intent, constraints, generated, artifact_dir, case_name+"-generated")
    # The unchanged real outline remains the authority even if the existing
    # solver's bounding-rectangle proposal is infeasible or has unknown inputs.
    for evaluation in generated_report["evaluations"]:
        if not evaluation["feasible"]:
            assert evaluation["candidate_id"] != generated_report["selected_candidate_id"] or generated_report["selection_status"] == "blocked"
    return {"family":family, "seed":seed, "fixture_sha256":fixture_hash, "expected_counterexample":expected,
            "components":len(context["components"]), "pads":len(context["pads"]),
            "decision":measured, "additional_cases":extra,
            "existing_solver":{**generated_summary, "generation_elapsed_ms":round(generator_ms,3),
                "generator_budget":{"candidate_count":1,"iterations":iterations,"seed":seed},
                "proposal_source":"existing eda_agent.placement.autoplace; proxy geometry only"}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=ROOT/"docs"/"PCB_LAYOUT_BENCHMARK_RESULTS.json")
    parser.add_argument("--seeds",default=",".join(map(str,HELD_OUT_SEEDS)))
    parser.add_argument("--iterations",type=int,default=8)
    parser.add_argument("--artifacts-dir",type=Path)
    args = parser.parse_args(argv)
    seeds = tuple(int(s) for s in args.seeds.split(","))
    if not seeds or len(seeds)>8 or len(set(seeds))!=len(seeds) or any(s<0 or s>2**31 for s in seeds):
        parser.error("Use 1..8 distinct nonnegative 32-bit seeds")
    if set(seeds) & set(DEVELOPMENT_SEEDS):
        parser.error("Held-out benchmark seeds must not overlap development seeds")
    if not 1 <= args.iterations <= 100:
        parser.error("iterations must be 1..100 for this bounded benchmark")
    sources = ["scripts/benchmark_pcb_layout.py", "eda-agent/src/eda_agent/design/placement_evidence.py", "eda-agent/src/eda_agent/design/placement_p0.py", "eda-agent/src/eda_agent/design/pcb_placement.py",
               "eda-agent/src/eda_agent/design/copper_geometry.py", "eda-agent/src/eda_agent/placement/autoplace.py"]
    source_hashes = {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources}
    start = time.perf_counter_ns()
    cases = [run_case(family,seed,args.iterations,args.artifacts_dir) for family in FAMILIES for seed in seeds]
    first_elapsed = (time.perf_counter_ns()-start)/1e6
    repeated = [run_case(family,seed,args.iterations,None) for family in FAMILIES for seed in seeds]
    def deterministic(value):
        if isinstance(value, dict):
            return {k:deterministic(v) for k,v in value.items() if k not in {"elapsed_ms", "generation_elapsed_ms"}}
        if isinstance(value, list):
            return [deterministic(v) for v in value]
        return value
    assert deterministic(cases) == deterministic(repeated), "Repeated fixture/decision/solver output differs"
    assert source_hashes == {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources}, "Source changed during the benchmark; rerun with frozen files"
    command = subprocess.run(["git","rev-parse","HEAD"],cwd=ROOT,capture_output=True,text=True,check=True)
    dirty = subprocess.run(["git","status","--porcelain=v1"],cwd=ROOT,capture_output=True,text=True,check=True)
    payload = {"schema":1, "policy_version":POLICY_VERSION,
        "benchmark_kind":"synthetic_decision_regression_and_existing_solver_smoke",
        "environment":{"python":platform.python_version(),"platform":platform.platform(),"cpu_count":os.cpu_count()},
        "source":{"git_head":command.stdout.strip(),"working_tree_dirty":bool(dirty.stdout.strip()),
                  "module_sha256":source_hashes},
        "held_out_seeds":list(seeds),"development_seeds":list(DEVELOPMENT_SEEDS),
        "held_out_scope":"Independent computational seeds of synthetic fixtures; not a held-out expert-layout dataset",
        "asserted_counterexamples_passed":True,"families":list(FAMILIES),"case_count":len(cases),
        "measurement_elapsed_ms":round((time.perf_counter_ns()-start)/1e6,3),
        "first_pass_elapsed_ms":round(first_elapsed,3),
        "reproducibility":{"verified":True,"runs":2,"compared":"fixture hashes, decisions, evidence/plan digests, metrics and artifact sizes; timings excluded"},
        "cases":cases,
        "unexecuted":{"native_cad":True,"expert_layout_comparison":True,"route_completion":True,
                      "native_drc":True,"via_optimization":True,"field_simulation":True},
        "limits":["No production PCB, actual part or copied package coordinates are used.",
            "Numeric fixture budgets are sourced synthetic engineering policies, not hardware recommendations.",
            "Channel width, HPWL and pin chords are estimates/proxies; generated proposals must pass hard evidence gates.",
            "Wall time depends on the machine; fixture/outcome digests and measured artifact bytes are recorded separately.",
            "These counterexamples do not establish optimizer superiority or manufacturing/SI/PI sign-off."]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(payload,sort_keys=True,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps({"output":str(args.output),"case_count":len(cases),"families":len(FAMILIES),
                      "elapsed_ms":payload["measurement_elapsed_ms"],"assertions_passed":True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
