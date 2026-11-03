"""Independent reproductions of managed placement review findings.

These reuse the synthetic protocol fixture; no Altium or user project runs.
"""
import asyncio
import copy

import pytest

from test_layout_protocol import service_fixture
from suite_layout import POLICY_VERSION
from eda_agent.design import placement_evidence as core


async def partial_recovery_preview(service, data, models, fabrication):
    context = await service.capture_context(data['board_path'], package_geometry=models,
                                            fabrication_profile=fabrication)
    plan = await service.plan_placement(context['context_id'], options={
        'recovery_mode': 'disk_and_pose_evidence', 'candidates': [
            {'id': 'shorter', 'moves': [{'component_id': '101', 'x_mm': 19}]}]})
    assert plan['selected_candidate_id'] == 'shorter'
    gate = await service.validate_plan(plan['plan_id'])
    assert gate['passed'], gate
    return context, plan, gate


@pytest.mark.parametrize('bound', [False, True])
def test_managed_scope_cannot_execute_eco_through_generic_button_press(tmp_path, bound):
    service, _, _, _, calls, backups = service_fixture(tmp_path)
    service._scope('a'*64, {'active': True, 'context_id': 'a'*64})
    args = {'button_caption': 'Execute Changes', 'allow_irreversible': True}
    if bound:
        args['recovery_binding'] = {'dialog_class': 'TChangeManagementForm',
            'dialog_title': 'Engineering Change Order', 'button_caption': 'Execute Changes',
            'token': 'untrusted-and-not-recovery'}
    with pytest.raises(ValueError, match='MANAGED_PLACEMENT_SCOPE'):
        service.guard_native_effect('eda', 'app_press_dialog_button', args)
    assert calls == backups == []


@pytest.mark.parametrize('drift', ['rule_values', 'drc_enabled', 'untargeted_pose', 'lock'])
def test_post_native_drift_cannot_qualify_a_different_board_state(tmp_path, drift):
    async def run():
        service, data, models, fab, calls, backups = service_fixture(tmp_path)
        data['native_rules']['rules'].append({'name': 'Width', 'enabled': True, 'drc_enabled': True})
        context, plan, gate = await partial_recovery_preview(service, data, models, fab)
        original = service.native_dispatch
        async def apply_then_drift(name, arguments):
            result = await original(name, arguments)
            if drift == 'rule_values':
                data['native_rule_values'].append({'width_mm': 9.99})
            elif drift == 'drc_enabled':
                data['native_rules']['rules'][0]['drc_enabled'] = False
            elif drift == 'lock':
                data['components'][0]['locked'] = True
            else:
                # .01mm preserves feasible geometry in the fixture. It is
                # nevertheless an unreviewed move outside the selected plan.
                data['components'][0]['x_mm'] += .01
                for pad in data['pads']:
                    if pad['component'] == data['components'][0]['designator']:
                        pad['x_mm'] += .01
            return result
        service.native_dispatch = apply_then_drift
        receipt = await service.apply_plan(plan['plan_id'], gate['validation_id'], 'review-drift')
        assert receipt['applied'], 'A completed native mutation must remain disclosed'
        assert not receipt['qualified_placement'], 'Feasibility alone cannot qualify a different planned state'
        assert len(calls) == len(backups) == 1
        assert not (await service.get_workflow(context['context_id']))['state']['complete']
        repeated = await service.apply_plan(plan['plan_id'], gate['validation_id'], 'review-drift')
        assert repeated['receipt_reused'] and not repeated['qualified_placement']
        assert len(calls) == len(backups) == 1, 'An unqualified result must never cause automatic replay'
    asyncio.run(run())


def test_observed_false_modified_flag_is_not_independent_clean_buffer_evidence(tmp_path):
    async def run():
        service, data, models, fab, calls, backups = service_fixture(tmp_path)
        data['document_modified'] = False
        data['document_modified_source'] = 'observed IServerDocument.Modified flag'
        context = await service.capture_context(data['board_path'], package_geometry=models, fabrication_profile=fab)
        plan = await service.plan_placement(context['context_id'], options={'candidates': [
            {'id': 'shorter', 'moves': [{'component_id': '101', 'x_mm': 19}]}]})
        assert plan['selected_candidate_id'] == 'shorter'
        gate = await service.validate_plan(plan['plan_id'])
        assert not gate['passed']
        assert any('recovery' in blocker or 'clean' in blocker for blocker in gate['unresolved_blockers'])
        with pytest.raises(ValueError):
            await service.apply_plan(plan['plan_id'], gate['validation_id'], 'unproven-clean')
        assert calls == backups == []
    asyncio.run(run())


@pytest.mark.parametrize('move_count', [500, 501])
def test_native_batch_budget_is_a_gate_before_checkpoint_or_operation_claim(tmp_path, monkeypatch, move_count):
    """Even an independently feasible proposal must respect transport bounds.

    Geometry evaluation is stubbed deliberately: this test isolates native
    dispatch budgeting, without running quadratic collision work on 501 parts.
    """
    async def run():
        service, data, models, fab, calls, backups = service_fixture(tmp_path)
        template = copy.deepcopy(data['components'][0])
        data['components'] = []
        data['pads'] = []
        models = {}
        moves = []
        for index in range(move_count):
            component = copy.deepcopy(template)
            component.update(id=str(1000+index), component_id=str(1000+index), designator='R'+str(index))
            data['components'].append(component)
            moves.append({'component_id': component['id'], 'designator': component['designator'],
                'x_mm': component['x_mm']+.1, 'y_mm': component['y_mm'],
                'rotation_deg': component['rotation_deg'], 'side': component['side']})
        context = await service.capture_context(data['board_path'], package_geometry=models, fabrication_profile=fab)
        plan_id = service._save('plans', dict(context_id=context['context_id'], intent={}, constraints=[],
            options={'recovery_mode': 'disk_and_pose_evidence'},
            comparison={'plan': {'moves': moves, 'candidate_id': 'feasible-batch', 'apply_eligible': True}},
            policy_version=POLICY_VERSION))
        monkeypatch.setattr(core, 'evaluate_placement', lambda *a, **k: dict(
            feasible=True, blockers=[], required_checks=['independent-feasible-geometry'], evidence_id='b'*64))
        gate = await service.validate_plan(plan_id)
        assert gate['passed'] is (move_count <= 500)
        assert calls == backups == []
        assert not (service.data_dir / 'operations').exists()
        if move_count > 500:
            assert 'native_transport_unsupported' in gate['unresolved_blockers']
            with pytest.raises(ValueError):
                await service.apply_plan(plan_id, gate['validation_id'], 'oversized-batch')
            assert calls == backups == []
            assert not (service.data_dir / 'operations').exists()
    asyncio.run(run())


@pytest.mark.parametrize('section', ['pads', 'board_outline', 'cutouts', 'regions'])
def test_partial_relevant_snapshot_coverage_is_a_hard_unknown(tmp_path, section):
    from test_layout_protocol import native_fixture
    from suite_layout import context_from_snapshot
    snapshot, models, fab = native_fixture(tmp_path)
    snapshot['complete'] = False
    snapshot['coverage'].update(pads='complete', board_outline='complete', regions='complete')
    snapshot['coverage'][section] = {'complete': False, 'status': 'partial'}
    context = context_from_snapshot(snapshot, 'partial-capture', models, fab, {})
    result = core.evaluate_placement(context)
    assert not result['feasible']
    required = 'manufacturing:pad_extraction' if section == 'pads' else 'outline:100'
    assert next(c for c in result['checks'] if c['id'] == required)['status'] == 'unknown'


def test_ring_presence_does_not_promote_missing_outline_coverage(tmp_path):
    from test_layout_protocol import native_fixture
    from suite_layout import context_from_snapshot
    snapshot, models, fab = native_fixture(tmp_path)
    snapshot['complete'] = False
    snapshot['coverage'].update(pads='complete', regions='complete')
    snapshot['coverage'].pop('board_outline', None)
    result = core.evaluate_placement(context_from_snapshot(snapshot, 'partial-capture', models, fab, {}))
    assert not result['feasible']
    assert next(c for c in result['checks'] if c['id'] == 'outline:100')['status'] == 'unknown'


def test_unrelated_incomplete_stackup_does_not_block_qualified_placement_geometry(tmp_path):
    from test_layout_protocol import native_fixture
    from suite_layout import context_from_snapshot
    snapshot, models, fab = native_fixture(tmp_path)
    snapshot['complete'] = False
    snapshot['coverage'].update(pads='complete', board_outline='complete', cutouts='complete', stackup='partial')
    result = core.evaluate_placement(context_from_snapshot(snapshot, 'partial-capture', models, fab, {}))
    assert result['feasible'], result['blockers']


def test_unreadable_cutout_record_cannot_pass_from_outer_ring(tmp_path):
    from test_layout_protocol import native_fixture
    from suite_layout import context_from_snapshot
    snapshot, models, fab = native_fixture(tmp_path)
    snapshot['complete'] = False
    snapshot['coverage'].update(pads='complete', board_outline='complete', cutouts='complete')
    snapshot['cutouts'].append({'id': 'unreadable', 'geometry_complete': False})
    result = core.evaluate_placement(context_from_snapshot(snapshot, 'partial-capture', models, fab, {}))
    assert not result['feasible']
    assert next(c for c in result['checks'] if c['id'] == 'outline:100')['status'] == 'unknown'


def test_free_board_pad_blocks_collision_without_fictional_package_binding(tmp_path):
    from test_layout_protocol import native_fixture
    from suite_layout import context_from_snapshot
    snapshot, models, fab = native_fixture(tmp_path)
    snapshot['pads'].append(dict(id='free-copper', x_mm=19, y_mm=25, net_name='OTHER',
        layer='Top', width_mm=.5, height_mm=.5, shape='rectangle', rotation_deg=0))
    context = core.validate_board_context(context_from_snapshot(snapshot, 'free-pad-capture', models, fab, {}))
    assert not any(p['id'] == 'free-copper' for p in context['pads'])
    assert context['stationary_pads'][0]['id'] == 'free-copper'
    assert 'component_id' not in context['stationary_pads'][0]
    assert core.evaluate_placement(context)['feasible']
    result = core.evaluate_placement(context, moves=[{'component_id': '101', 'x_mm': 19}])
    assert not result['feasible']
    assert any(c['status'] == 'fail' and 'free-copper' in c.get('scope', {}).get('pads', [])
               for c in result['checks'])
    from jsonschema import Draft202012Validator
    import json
    Draft202012Validator(core.contract_schemas()['BoardContext']).validate(json.loads(json.dumps(context)))


def test_unresolved_nonblank_pad_owner_cannot_be_certified_as_static_free_copper(tmp_path):
    from test_layout_protocol import native_fixture
    from suite_layout import context_from_snapshot
    snapshot, models, fab = native_fixture(tmp_path)
    snapshot['pads'].append(dict(id='unbound', component='unreadable-package', x_mm=19, y_mm=30,
        net_name='OTHER', layer='Top', width_mm=.5, height_mm=.5, shape='rectangle'))
    result = core.evaluate_placement(context_from_snapshot(snapshot, 'capture', models, fab, {}))
    assert not result['feasible']
    assert next(c for c in result['checks'] if c['id'] == 'manufacturing:pad_extraction')['status'] == 'unknown'
