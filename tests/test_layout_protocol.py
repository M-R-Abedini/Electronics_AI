"""Placement protocol invariants; synthetic data only, no Altium process."""
import asyncio
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from suite_layout import LayoutService, capture_digest, context_from_snapshot, list_tools
from suite_layout_policy import native_effects
from eda_agent.design import placement_evidence as core
from eda_agent.design.copper_snapshot import empty_snapshot
from suite_checkpoints import CheckpointStore

SOURCE = {'kind': 'engineering_policy', 'id': 'synthetic-fixture', 'revision': '1'}


def native_fixture(tmp_path):
    context = core.example_board_context()
    path = tmp_path / 'scratch.PcbDoc'
    path.write_bytes(b'saved fixture, not a production Altium file')
    snapshot = empty_snapshot(str(path), source='live')
    snapshot.update(complete=True, missing=[], board_identity='777', native_snapshot_token='capture_1',
        document_modified=False, board_outline=context['outline'], native_rules={'rules': []},
        native_rule_values=[], native_rule_coverage='supported_fields',
        coverage={section: 'complete' for section in snapshot['coverage']},
        layers=[{'name': 'Top', 'order': 0, 'kind': 'signal'}, {'name': 'Bottom', 'order': 1, 'kind': 'signal'}],
        nets=['SIGNAL'])
    models = {}
    for i, comp in enumerate(context['components']):
        cid = str(100 + i)
        snapshot['components'].append(dict(id=cid, component_id=cid, designator=comp['reference'],
            x_mm=comp['x_mm'], y_mm=comp['y_mm'], rotation_deg=0, side='top', layer='Top', locked=False))
        snapshot['pads'].append(dict(id=str(200+i), component=comp['reference'], pad_name='1',
            x_mm=comp['x_mm'], y_mm=comp['y_mm'], net_name='SIGNAL', layer='Top',
            width_mm=.5, height_mm=.5, shape='rectangle', rotation_deg=0))
        models[cid] = {'courtyard_local': comp['courtyard_local'], 'courtyard_source': SOURCE}
    return snapshot, models, context['fabrication_profile']


def service_fixture(tmp_path, *, fail_native=False, lie_about_moves=False):
    snapshot, models, fab = native_fixture(tmp_path)
    calls, checkpoints = [], []
    async def reader(*args, **kwargs):
        value = copy.deepcopy(snapshot)
        value['capture_metrics'] = {'elapsed_ms': len(calls) + 42}
        return value
    async def native(name, args):
        calls.append((name, copy.deepcopy(args)))
        if fail_native:
            return {'success': False, 'applied_ids': [args['moves'][0]['component_id']],
                    'possible_partial_native_edit': True, 'automatic_retry': False}
        if not lie_about_moves:
            for move in args['moves']:
                comp = next(c for c in snapshot['components'] if c['id'] == move['component_id'])
                dx, dy = move['x_mm']-comp['x_mm'], move['y_mm']-comp['y_mm']
                for pad in snapshot['pads']:
                    if pad['component'] == comp['designator']:
                        pad['x_mm'] += dx; pad['y_mm'] += dy
                comp.update({k: move[k] for k in ('x_mm', 'y_mm', 'rotation_deg', 'side')})
            snapshot['document_modified'] = True
        return {'success': True, 'applied_ids': [m['component_id'] for m in args['moves']]}
    def checkpoint(files):
        checkpoints.append(files)
        return CheckpointStore(tmp_path / 'recovery').create(files)
    service = LayoutService(tmp_path / 'data', native_dispatch=native, snapshot_reader=reader, checkpoint=checkpoint)
    return service, snapshot, models, fab, calls, checkpoints


async def preview(service, snapshot, models, fab):
    context = await service.capture_context(snapshot['board_path'], package_geometry=models, fabrication_profile=fab)
    plan = await service.plan_placement(context['context_id'], options={'recovery_mode': 'disk_and_pose_evidence', 'candidates': [
        {'id': 'shorter', 'moves': [{'component_id': '101', 'x_mm': 19}]}]})
    assert plan['selected_candidate_id'] == 'shorter'
    gate = await service.validate_plan(plan['plan_id'])
    return context, plan, gate


def test_exact_saved_plan_checkpoint_once_and_replay_without_optimizer(tmp_path, monkeypatch):
    async def run():
        service, data, models, fab, calls, backups = service_fixture(tmp_path)
        context, plan, gate = await preview(service, data, models, fab)
        assert gate['passed'], gate
        monkeypatch.setattr(core, 'generate_placement_candidates', lambda *a: pytest.fail('optimizer rerun during apply'))
        result = await service.apply_plan(plan['plan_id'], gate['validation_id'], 'one_operation')
        assert result['applied'] and result['qualified_placement']
        assert len(calls) == len(backups) == 1
        assert calls[0][1]['moves'] == plan['plan']['moves']
        assert not result['recovery']['unsaved_memory_captured']
        repeated = await service.apply_plan(plan['plan_id'], gate['validation_id'], 'one_operation')
        assert repeated['receipt_reused'] and not repeated['current_freshness_verified']
        assert len(calls) == len(backups) == 1
        state = await service.get_workflow(context['context_id'])
        assert state['state']['complete']
    asyncio.run(run())


@pytest.mark.parametrize('change', ['pose', 'rule', 'lock', 'dirty'])
def test_freshness_checks_actual_memory_rules_locks_before_checkpoint(tmp_path, change):
    async def run():
        service, data, models, fab, calls, backups = service_fixture(tmp_path)
        _, plan, gate = await preview(service, data, models, fab)
        if change == 'pose': data['components'][0]['x_mm'] += .1
        if change == 'rule': data['native_rule_values'].append({'width_mm': .23})
        if change == 'lock': data['components'][0]['locked'] = True
        if change == 'dirty': data['document_modified'] = True
        with pytest.raises(ValueError, match='STALE_PLACEMENT'):
            await service.apply_plan(plan['plan_id'], gate['validation_id'], 'changed')
        assert not calls and not backups
    asyncio.run(run())


def test_missing_critical_evidence_cannot_be_passed_by_caller_journal(tmp_path):
    async def run():
        service, data, models, fab, calls, _ = service_fixture(tmp_path)
        context = await service.capture_context(data['board_path'], package_geometry=models)
        plan = await service.plan_placement(context['context_id'], options={'candidates': []})
        journal = service._journal(context['context_id'])
        journal.stage_result('placement', 'ok')
        journal.stage_result('verification', 'ok')
        assert not (await service.get_workflow(context['context_id']))['state']['complete']
        gate = await service.validate_plan(plan['plan_id'])
        assert not gate['passed'] and gate['unresolved_blockers']
        with pytest.raises(ValueError, match='passing server-generated'):
            await service.apply_plan(plan['plan_id'], gate['validation_id'], 'fake_stage_ok')
        assert not calls
    asyncio.run(run())


@pytest.mark.parametrize('lie', [True, False])
def test_partial_or_wrong_native_readback_never_qualifies_or_retries(tmp_path, lie):
    async def run():
        service, data, models, fab, calls, _ = service_fixture(tmp_path,
            fail_native=not lie, lie_about_moves=lie)
        _, plan, gate = await preview(service, data, models, fab)
        receipt = await service.apply_plan(plan['plan_id'], gate['validation_id'], 'partial')
        assert not receipt['qualified_placement']
        assert receipt['post_pose_mismatch_ids'] if lie else receipt['possible_partial_native_edit']
        await service.apply_plan(plan['plan_id'], gate['validation_id'], 'partial')
        assert len(calls) == 1
    asyncio.run(run())


def test_offline_artifacts_and_geometry_models_cannot_forge_native_authority(tmp_path):
    async def run():
        service = LayoutService(tmp_path)
        context = await service.capture_context(board_context=core.example_board_context())
        plan = await service.plan_placement(context['context_id'], options={'candidates': []})
        gate = await service.validate_plan(plan['plan_id'])
        assert 'trusted_live_capture' in gate['unresolved_blockers']
        assert not gate['passed']
        assert not service.active_scopes()
    asyncio.run(run())


def test_managed_scope_guards_nested_alias_and_opaque_effects(tmp_path):
    service = LayoutService(tmp_path)
    cid = 'a'*64
    service._scope(cid, dict(active=True, context_id=cid))
    for backend, name, args in [
        ('eda', 'pcb_batch_move_components', {'moves': []}),
        ('eda', 'tool_invoke', {'name': 'obj_modify_objects', 'arguments': {'properties': {'X': 9}}}),
        ('legacy', 'run_altium_script', {'script': "ResultText := 'purpose:read';"}),
        ('eda', '_suite_copper_apply_rules', {}),
    ]:
        with pytest.raises(ValueError, match='MANAGED_PLACEMENT_SCOPE'):
            service.guard_native_effect(backend, name, args)
    service.guard_native_effect('eda', 'pcb_get_components', {})
    assert native_effects('eda', 'tool_invoke', {'name': 'pcb_get_pads', 'arguments': {}}) == set()


def test_contracts_and_registered_api_are_json_schema_valid():
    assert len(list_tools()) == 8
    for tool in list_tools(): Draft202012Validator.check_schema(tool['inputSchema'])
    for name, schema in core.contract_schemas().items():
        if isinstance(schema, dict): Draft202012Validator.check_schema(schema)

def test_live_eco_capture_preserves_explicit_scope_in_post_validation(tmp_path, monkeypatch):
    async def run():
        service, data, models, fab, calls, backups = service_fixture(tmp_path)
        captured = await service.capture_context(data['board_path'], package_geometry=models,
            fabrication_profile=fab, scope_component_ids=['101'])
        assert captured['context']['scope_component_ids'] == ['101']
        plan = await service.plan_placement(captured['context_id'], options={
            'recovery_mode': 'disk_and_pose_evidence',
            'candidates': [{'id': 'eco', 'moves': [{'component_id': '101', 'x_mm': 19}]}]})
        gate = await service.validate_plan(plan['plan_id'])
        assert gate['passed']
        seen = []
        original = core.evaluate_placement
        def evaluate(context, *args, **kwargs):
            seen.append(context.get('scope_component_ids'))
            return original(context, *args, **kwargs)
        monkeypatch.setattr(core, 'evaluate_placement', evaluate)
        result = await service.apply_plan(plan['plan_id'], gate['validation_id'], 'scoped')
        assert result['qualified_placement']
        assert seen and all(scope==['101'] for scope in seen)
    asyncio.run(run())


def test_routine_mcp_checks_are_bounded_without_hiding_failed_summary():
    from suite_layout import bounded_checks
    checks = [{'id': str(i), 'status': 'pass'} for i in range(100)]
    checks.append({'id': 'bad', 'status': 'unknown'})
    result = bounded_checks({'report': {'checks': checks, 'feasible': False}})['report']
    assert result['checks_total'] == 101 and result['checks_truncated']
    assert len(result['checks']) == 64 and result['checks'][0]['id'] == 'bad'
    assert not result['feasible'] and len(checks) == 101

def test_old_policy_plan_cannot_be_revalidated_or_dispatched(tmp_path):
    async def run():
        service, data, models, fab, calls, backups = service_fixture(tmp_path)
        context, plan, gate = await preview(service, data, models, fab)
        old = service._load('plans', plan['plan_id'])
        old['policy_version']='placement-evidence-v1'
        old_id=service._save('plans',old)
        with pytest.raises(ValueError,match='STALE_PLACEMENT_POLICY'):
            await service.validate_plan(old_id)
        # Even a copied previously-passing gate cannot authorize this plan.
        copied=service._load('validations',gate['validation_id']);copied['plan_id']=old_id
        copied_id=service._save('validations',copied)
        with pytest.raises(ValueError,match='STALE_PLACEMENT_POLICY'):
            await service.apply_plan(old_id,copied_id,'old_policy')
        assert calls==backups==[]
    asyncio.run(run())

def test_generic_pcb_input_gate_reads_real_directory_before_any_native_call(tmp_path):
    async def run():
        service, data, models, fab, calls, backups = service_fixture(tmp_path)
        project=tmp_path/'empty-design';project.mkdir()
        result=await service.call_tool_async('capture_context',{'input_gate':{'project_dir':str(project),
            'safety_requirements':{'isolation_required':True,'barriers':[]}}})
        assert result['context_id'] is None and not result['suitable_as_native_precondition']
        assert set(result['input_gate']['blockers'])=={'blocked_missing_board_artifact','blocked_missing_safety_input'}
        assert calls==backups==[] and list(project.iterdir())==[]
    asyncio.run(run())
