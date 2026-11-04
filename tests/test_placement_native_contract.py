"""Plan-bound placement validates a complete batch before native dispatch.

Pure transport tests exercise the shipping validator and private wrapper;
native safety contracts additionally guard ordering in the DelphiScript path.
Actual SDK moves require scratch-board acceptance, never a production board.
"""
import ast
import asyncio
import copy
from pathlib import Path

import pytest

from suite_placement_adapter import MAX_ABSOLUTE_MM, MAX_MOVES, prepare_layout_native_batch

ROOT = Path(__file__).resolve().parents[1]


def move(identifier='1234', name='C1', **changes):
    return dict(component_id=identifier, designator=name, x_mm=12.12345678,
                y_mm=-.00000254, rotation_deg=270.123456, side='top', **changes)


def arguments(moves=None):
    return dict(board_path='D:/scratch/placement.PcbDoc', board_identity='424242',
                expected_native_snapshot_token='captured-native-token',
                moves=moves if moves is not None else [move()], operation_id='operation-123')


def test_native_batch_retains_fractional_coordinates_and_canonical_shape():
    result = prepare_layout_native_batch(**arguments())
    assert result['move_count'] == 1
    assert result['move_0'] == {'component_id': '1234', 'designator': 'C1',
        'x_mm': '12.12345678', 'y_mm': '-0.00000254', 'rotation_deg': '270.123456', 'side': 'top'}
    assert float(result['move_0']['y_mm']) == -.00000254
    assert not {'moves', 'save', 'flip', 'allow_locked'} & result.keys()


@pytest.mark.parametrize('field,value', [
    ('x_mm', float('nan')), ('y_mm', float('inf')), ('rotation_deg', -float('inf')),
    ('x_mm', True), ('y_mm', '10'), ('x_mm', 10**1000),
    ('x_mm', MAX_ABSOLUTE_MM + .01), ('y_mm', -MAX_ABSOLUTE_MM - .01),
    ('rotation_deg', -1), ('rotation_deg', 360), ('side', 'mirror'),
    ('component_id', '../123'), ('component_id', '0'), ('designator', ''),
])
def test_invalid_tail_is_rejected_as_a_whole_batch(field, value):
    invalid = move('5678', 'C2')
    invalid[field] = value
    with pytest.raises(ValueError):
        prepare_layout_native_batch(**arguments([move(), invalid]))


@pytest.mark.parametrize('changes', [
    {'operation_id': '../operation'}, {'expected_native_snapshot_token': 'a/b'},
    {'board_identity': 'not-native'}, {'board_path': 'relative.PcbDoc'},
    {'board_path': 'D:/scratch/board.SchDoc'}, {'moves': []},
    {'moves': [move()] * (MAX_MOVES + 1)},
    {'moves': [move(), move('1234', 'C2')]},
    {'moves': [move(), move('5678', 'c1')]},
])
def test_bound_identity_and_ambiguity_checks(changes):
    args = arguments()
    args.update(changes)
    with pytest.raises(ValueError):
        prepare_layout_native_batch(**args)


def test_unknown_move_fields_cannot_request_unlocking_or_flips():
    args = arguments()
    args['moves'][0]['allow_locked'] = True
    with pytest.raises(ValueError, match='exactly'):
        prepare_layout_native_batch(**args)


def shipping_private_wrapper(name='_suite_layout_apply_plan'):
    # Importing eda_stdio starts a configured server/installs coordination.
    # Bind only its exact shipping function AST to a capturing decorator.
    source = (ROOT / 'eda_stdio.py').read_text(encoding='utf-8')
    node = next(node for node in ast.parse(source).body
                if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
    class Mcp:
        def tool(self):
            return lambda fn: fn
    scope = {'mcp': Mcp()}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROOT / 'eda_stdio.py'), 'exec'), scope)
    return scope[node.name]


def test_private_wrapper_dispatches_once_and_never_dispatches_invalid_tail(monkeypatch):
    from eda_agent import bridge
    calls = []
    class FakeBridge:
        async def send_command_async(self, command, params, timeout):
            calls.append((command, params, timeout))
            return {'success': True, 'operation_id': params['operation_id']}
    monkeypatch.setattr(bridge, 'get_bridge', lambda: FakeBridge())
    wrapper = shipping_private_wrapper()
    result = asyncio.run(wrapper(**arguments()))
    assert result['success'] and len(calls) == 1
    assert calls[0][0] == 'pcb.apply_placement_plan' and calls[0][2] == 180
    assert calls[0][1]['move_0']['x_mm'] == '12.12345678'
    bad = arguments([move(), move('5678', 'C2')])
    bad['moves'][-1]['x_mm'] = float('nan')
    with pytest.raises(ValueError):
        asyncio.run(wrapper(**bad))
    assert len(calls) == 1, 'An invalid batch tail must not dispatch even its valid prefix'


def native_handler():
    source = (ROOT / 'eda-agent/scripts/altium/PCB.pas').read_text(encoding='utf-8')
    return source.split('Function PCB_ApplyPlacementPlan(', 1)[1].split('Function HandlePCBCommand(', 1)[0]


def test_native_gate_order_and_separate_immutable_operation_marker():
    source = native_handler()
    duplicate = source.index('Ids.IndexOf(Id) >= 0')
    lock = source.index('If Not Comp.Moveable Then')
    fresh = source.index('Fresh <> Expected')
    claim = source.index('WriteFileContent(ClaimPath, Claim)')
    mutate = source.index('PCBM_BeginModify')
    assert duplicate < lock < fresh < claim < mutate
    assert source.index('Trim(ReadFileContent(ClaimPath)) <> Claim') < mutate
    assert 'DeleteFile(ClaimPath)' not in source and source.count('WriteFileContent(ClaimPath') == 1
    assert source.index('If Reused Then Begin') < source.index('If Not Comp.Moveable Then')
    assert 'LAYOUT_OPERATION_UNKNOWN' in source and 'LAYOUT_OPERATION_CONFLICT' in source
    assert "ExtractJsonValue(Reply, 'data')" not in source
    assert 'PCBM_EndModify' in source and 'Finally PCBServer.PostProcess' in source
    assert source.index('final batch-wide readback') > mutate
    assert 'DoFileSave' not in source and 'SaveOneDoc' not in source and 'DoEvents' not in source
    for field in ('applied_ids', 'failed_ids', 'unverified_ids', 'not_attempted_ids',
                  'possible_partial_native_edit', 'native_rollback_available', 'durable_receipt_saved'):
        assert field in source


def test_snapshot_proof_includes_supported_rules_locks_and_physical_metadata():
    source = (ROOT / 'eda-agent/scripts/altium/PCB.pas').read_text(encoding='utf-8')
    snapshot = source.split('Function PCB_GetCopperSnapshot(', 1)[1].split('Function PCB_FocusCopperViolation(', 1)[0]
    assert "JsonBool('locked', Not Comp.Moveable)" in snapshot
    assert "JsonStr('component_id', IntToStr(Comp.I_ObjectAddress))" in snapshot
    assert 'Comp.BoundingRectangleNoNameComment' in snapshot
    assert 'courtyard_coverage' in snapshot and 'unknown' in snapshot
    assert 'PCB_DesignRulesData(Board)' in snapshot and 'Copper_RuleValues(Board)' in snapshot
    assert 'SnapshotDoc.Modified' in snapshot and "DirtyJson := 'null'" in snapshot
    proof = snapshot.split("Item := '{\"schema\":1", 1)[1].split('If Length(Item)', 1)[0]
    assert 'native_rules' in proof and 'native_rule_values' in proof
    assert 'document_modified' in proof and 'elapsed_ms' not in proof


def test_native_proof_records_separate_drc_participation_with_unknown_fallback():
    source = (ROOT / 'eda-agent/scripts/altium/PCB.pas').read_text(encoding='utf-8')
    builder = source.split('Function PCB_DesignRulesData(', 1)[1].split('Function PCB_GetDesignRules(', 1)[0]
    assert 'BoolToJsonStr(Rule.Enabled)' in builder
    assert 'BoolToJsonStr(Rule.DRCEnabled)' in builder
    assert "DrcEnabledJson := 'null'" in builder
    assert "'\"drc_enabled\":' + DrcEnabledJson" in builder

def test_fast_private_packet_keeps_expected_pose_local_and_rejects_invalid_tail(monkeypatch):
    from eda_agent import bridge
    calls=[]
    class FakeBridge:
        async def send_command_async(self,command,params,timeout):
            calls.append((command,params,timeout));return {'success':True}
    monkeypatch.setattr(bridge,'get_bridge',lambda:FakeBridge())
    wrapper=shipping_private_wrapper('_suite_layout_fast_pose')
    args=arguments();args.pop('expected_native_snapshot_token');args['expected_poses']=copy.deepcopy(args['moves'])
    args['expected_poses'][0]['x_mm']+=.127
    assert asyncio.run(wrapper(**args))['success']
    assert calls[0][1]['move_0']['expected_x_mm']!=calls[0][1]['move_0']['x_mm']
    assert 'expected_pose_0' not in calls[0][1] and calls[0][0]=='pcb.apply_placement_plan'
    args['moves'][0]['x_mm']=float('nan')
    with pytest.raises(ValueError):asyncio.run(wrapper(**args))
    assert len(calls)==1
