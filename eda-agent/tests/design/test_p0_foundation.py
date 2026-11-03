"""P0 regressions reusing the pre-existing placement test fixtures."""
import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import pytest
from eda_agent.design.placement_evidence import compare_placement_candidates, evaluate_placement
from eda_agent.design.placement_p0 import transformed_pad_aabb_manhattan_mm
from eda_agent.design.project_gate import evaluate_project_gate
from eda_agent.design.session import SessionStore, STAGES, STATUS_OK
from eda_agent.bridge.targeting import TARGET, bind_target, targeted_tool
from eda_agent.tools.registry import ToolRegistry

fixture_path = Path(__file__).with_name('test_placement_evidence.py')
spec = importlib.util.spec_from_file_location('existing_placement_fixtures',fixture_path)
fixtures = importlib.util.module_from_spec(spec);spec.loader.exec_module(fixtures)


@pytest.mark.parametrize('delta', [.1, -.1])
def test_existing_decap_fixture_prohibits_each_leg_regression_even_when_sum_ties(delta):
    context, intent = fixtures.decoupling_fixture()
    intent['via_transition_penalty_mm'] = .25  # Explicit synthetic proxy parameter, not a hardware limit.
    result = compare_placement_candidates(context,intent,candidates=[{'id':'shift', 'moves':[{'component_id':'b','x_mm':14+delta}]}])
    before,after = result['evaluations']
    worse = 'vcc' if delta>0 else 'gnd'
    key = 'critical_decap:local_bypass:'+worse
    assert after['metrics'][key]['value']-before['metrics'][key]['value'] == pytest.approx(.1)
    assert result['selected_candidate_id']=='baseline' and result['plan']['moves']==[]
    assert any(r['metric']==key for r in result['tradeoffs'][1]['critical_regressions'])
    assert context['components'][1]['x_mm']==14


def test_existing_fixture_rejects_tier_zero_failure_and_visual_only_improvement():
    c=fixtures.example_board_context()
    result=compare_placement_candidates(c,candidates=[{'id':'outside','moves':[{'component_id':'b','x_mm':100}]}])
    assert result['selected_candidate_id']=='baseline' and not result['evaluations'][1]['feasible']
    result=compare_placement_candidates(c,candidates=[])
    assert result['selection_status']=='baseline_retained' and result['plan']['moves']==[]


def test_positive_tier_one_tolerance_cannot_be_requested():
    c=fixtures.example_board_context()
    with pytest.raises(ValueError,match='no positive'):
        compare_placement_candidates(c,{'critical_paths':[{'id':'p','pad_ids':['a.1','b.1']}]},selection_policy={'critical_nonregression_mm':{'critical_path:p':.1}})


def test_round_and_rotated_pad_aabbs_have_defined_proxy_and_declared_layer_penalty():
    a={'outline':[[-1,-1],[1,-1],[1,1],[-1,1]],'layer':'Top'}
    b={'outline':[[3,4],[4,3],[5,4],[4,5]],'layer':'Top'}
    assert transformed_pad_aabb_manhattan_mm(a,b)==4
    b['layer']='Bottom'
    assert transformed_pad_aabb_manhattan_mm(a,b) is None
    assert transformed_pad_aabb_manhattan_mm(a,b,.7)==pytest.approx(4.7)


def test_actual_empty_directory_and_incomplete_isolation_cannot_be_overridden_by_ok(tmp_path):
    report=evaluate_project_gate(str(tmp_path), {'isolation_required':True,'barriers':[{'id':'IN-OUT','working_voltage':'TBD'}]})
    assert not list(tmp_path.iterdir()),'Gate must not fabricate a PCB artifact'
    assert set(report['blockers'])=={'blocked_missing_board_artifact','blocked_missing_safety_input'}
    assert not report['passed'] and report['target_board_path'] is None
    journal=SessionStore(tmp_path/'journal').start('input review',session_id='p0')
    journal.verified_gate('placement',report['evidence_id'],report['evidence_id'],'p0',passed=False,checks=report['blockers'])
    for stage in STAGES:journal.stage_result(stage,STATUS_OK)
    assert not journal.state().complete and journal.state().next_stage=='placement'


def test_gate_status_is_computed_from_directory_and_declared_scope(tmp_path):
    assert evaluate_project_gate(str(tmp_path),{'isolation_required':False})['blockers']==['blocked_missing_board_artifact']
    with pytest.raises(ValueError):evaluate_project_gate(str(tmp_path),{})


def test_request_local_target_is_in_real_schema_and_retained_across_async_call():
    seen=[]
    async def sample(x:int=1):
        await asyncio.sleep(0)
        seen.append(bind_target({'x':x}))
        return x
    registry=ToolRegistry();registry.tool()(targeted_tool(sample))
    assert 'target_board_path' in registry.get('sample').inputSchema['properties']
    assert asyncio.run(registry.call_tool('sample',{'target_board_path':r'D:\scratch\test.PcbDoc'}))==1
    assert seen[0]['target_board_path']==r'D:\scratch\test.PcbDoc' and TARGET.get() is None
    token=TARGET.set(r'D:\scratch\test.PcbDoc')
    try:
        with pytest.raises(ValueError,match='ERR_TARGET_DOCUMENT_MISMATCH'):bind_target({'board_path':r'D:\other.PcbDoc'})
    finally:TARGET.reset(token)
