"""SI/PI orchestration, provenance and native admission regression tests."""
import asyncio
import copy
import json
from pathlib import Path

import pytest

from suite_si_pi import SiPiService, prepare_native_rule_batch, list_tools


def snapshot(tmp_path):
    return {'schema': 1, 'units': 'mm', 'board_path': str(tmp_path / 'board.PcbDoc'),
        'source': 'live', 'complete': True, 'missing': [],
        'board_identity': 'fixture-board', 'native_snapshot_json': '{"native_fixture":true}',
        'coverage': {s: 'complete' for s in ('layers', 'tracks', 'arcs', 'vias', 'pads', 'regions', 'keepouts', 'net_ties', 'components', 'nets')},
        'layers': [{'name': 'Top', 'kind': 'signal', 'order': 0},
                   {'name': 'Plane', 'kind': 'plane', 'order': 1, 'reference_net': 'GND'}],
        'tracks': [{'id': 'a', 'net_name': 'A', 'layer': 'Top', 'start': [0, .3], 'end': [10, .3], 'width_mm': .1},
                   {'id': 'sw', 'net_name': 'SW', 'layer': 'Top', 'start': [0, 0], 'end': [10, 0], 'width_mm': .2}],
        'arcs': [], 'vias': [], 'pads': [], 'regions': [{'id': 'g', 'net_name': 'GND', 'layer': 'Plane',
            'kind': 'plane', 'poured': True, 'outline': [[-2,-2],[12,-2],[12,2],[-2,2]], 'holes': []}],
        'keepouts': [], 'net_ties': [], 'components': [], 'nets': ['A', 'SW', 'GND']}


ROLES = {'net_roles': {'A': 'sensitive_analog', 'SW': 'switch', 'GND': 'ground'}}


def test_service_audit_uses_real_geometry_and_report_bound_focus(tmp_path):
    async def run():
        data = snapshot(tmp_path)
        seen = []
        async def reader(*args, **kwargs):
            return copy.deepcopy(data)
        async def native(name, args):
            seen.append((name, args))
            return {'success': True, 'focused': True}
        service = SiPiService(tmp_path / 'state', native_dispatch=native, snapshot_reader=reader)
        report = await service.audit_copper(snapshot=data, topology=ROLES)
        violation = next(v for v in report['violations'] if v['check'] == 'parallel_coupling')
        assert violation['net_name'] == 'A' and 0 <= violation['x_mm'] <= 10
        assert violation['measured_value'] < violation['limit_value']
        focused = await service.focus_violation(report['report_id'], violation['id'])
        assert focused['focused'] and seen[-1][0] == '_suite_copper_focus'
        assert seen[-1][1]['net_names'] == ['A']
        data['tracks'][0]['start'][1] = 2
        with pytest.raises(ValueError, match='stale'):
            await service.focus_violation(report['report_id'], violation['id'])
        assert len(seen) == 1
    asyncio.run(run())


def test_native_apply_requires_live_preview_unchanged_copper_and_preserves_decimal_rules(tmp_path):
    async def run():
        data = snapshot(tmp_path)
        writes = []
        async def reader(*args, **kwargs):
            return copy.deepcopy(data)
        async def native(name, args):
            writes.append((name, args))
            return {'success': True, 'applied_count': len(args['rules'])}
        service = SiPiService(tmp_path / 'state', native_dispatch=native, snapshot_reader=reader)
        kwargs = dict(board_path=data['board_path'], source='live', topology=ROLES,
                      net_constraints={'A': {'required_width_mils': 7.125}})
        preview = await service.synthesize_and_apply_rules(**kwargs)
        assert not writes and not preview['board_modified']
        result = await service.synthesize_and_apply_rules(**kwargs, apply=True,
            expected_snapshot_id=preview['snapshot_id'], expected_preview_id=preview['preview_id'])
        assert result['applied'] and writes[-1][0] == '_suite_copper_apply_rules'
        assert any(r['value'] == 7.125 for r in writes[-1][1]['rules'])
        assert any('scope2' in r for r in writes[-1][1]['rules'])
        assert writes[-1][1]['expected_native_snapshot'] == data['native_snapshot_json']
        with pytest.raises(ValueError, match='Rules or options changed'):
            await service.synthesize_and_apply_rules(**{**kwargs, 'net_constraints': {'A': {'required_width_mils': 100}}},
                apply=True, expected_snapshot_id=preview['snapshot_id'], expected_preview_id=preview['preview_id'])
        with pytest.raises(ValueError, match='expected_preview_id'):
            await service.synthesize_and_apply_rules(**kwargs, apply=True, expected_snapshot_id=preview['snapshot_id'])
        data['tracks'][0]['end'][0] = 11
        with pytest.raises(ValueError, match='changed'):
            await service.synthesize_and_apply_rules(**kwargs, apply=True,
                expected_snapshot_id=preview['snapshot_id'])
        assert len(writes) == 1
        with pytest.raises(ValueError, match='fresh live'):
            await service.synthesize_and_apply_rules(snapshot=data, topology=ROLES,
                apply=True, expected_snapshot_id=preview['snapshot_id'])
    asyncio.run(run())


def test_native_rule_batch_rejects_invalid_tail_before_any_dispatch(tmp_path):
    target = str(tmp_path / 'board.PcbDoc')
    valid = {'name': 'width', 'rule_type': 'width', 'value': 6.125, 'max_value': 8, 'favored_value': 7}
    result = prepare_native_rule_batch(target, [{'name': 'A', 'net_names': ['NET,1', 'NET"2']}], [valid])
    assert result['rule_0']['value'] == 6.125 and result['class_0']['nets'] == '"NET,1","NET""2"'
    for invalid in (dict(valid, value=float('nan')), dict(valid, max_value=3),
                    dict(valid, rule_type='arbitrary_script'), dict(valid, favored_value=10)):
        with pytest.raises(ValueError):
            prepare_native_rule_batch(target, [], [valid, invalid])


def test_incomplete_evidence_never_silently_applies_and_reports_are_integrity_checked(tmp_path):
    async def run():
        data = snapshot(tmp_path)
        data.update(complete=False, missing=['regions incomplete'])
        async def reader(*args, **kwargs): return copy.deepcopy(data)
        async def native(*args): raise AssertionError('No native write should occur')
        service = SiPiService(tmp_path, native_dispatch=native, snapshot_reader=reader)
        preview = await service.synthesize_and_apply_rules(board_path=data['board_path'], topology=ROLES)
        with pytest.raises(ValueError, match='Incomplete'):
            await service.synthesize_and_apply_rules(board_path=data['board_path'], topology=ROLES,
                apply=True, expected_snapshot_id=preview['snapshot_id'], expected_preview_id=preview['preview_id'])
        report = await service.audit_copper(snapshot=data, topology=ROLES)
        assert not report['coverage_complete']
        path = service.data_dir / 'reports' / (report['report_id'] + '.json')
        path.write_text('{}')
        with pytest.raises(ValueError, match='checksum'):
            await service.focus_violation(report['report_id'], report['violations'][0]['id'])
    asyncio.run(run())


def test_references_pairs_and_classes_preflight_before_native_dispatch(tmp_path):
    target = str(tmp_path / 'board.PcbDoc')
    for groups, pairs in (
        ([{'name': 'A', 'net_names': ['A']}, {'name': 'A', 'net_names': ['B']}], []),
        ([{'name': 'A', 'net_names': ['A', 'A']}], []),
        ([], [{'name': 'pair', 'nets': ['A', 'A']}]),
        ([], [{'name': 'pair', 'nets': ['A', 'B']}, {'name': 'pair', 'nets': ['C', 'D']}]),
    ):
        with pytest.raises(ValueError, match='unique|distinct'):
            prepare_native_rule_batch(target, groups, [], pairs)
    async def run():
        service = SiPiService(tmp_path)
        for topology in ({'net_roles': {'PHANTOM': 'ground'}}, {'constraints': {'PHANTOM': {'current_a': 3}}},
                         {'differential_pairs': [{'name': 'pair', 'nets': ['A', 'PHANTOM']}]}):
            with pytest.raises(ValueError, match='uncaptured net'):
                await service.classify_and_partition(snapshot=snapshot(tmp_path), topology=topology)
    asyncio.run(run())


def test_class_ownership_persists_native_partial_result_but_is_not_caller_supplied(tmp_path):
    async def run():
        data = snapshot(tmp_path)
        writes = []
        async def reader(*args, **kwargs): return copy.deepcopy(data)
        async def native(name, args):
            writes.append(args)
            return {'success': False, 'applied_count': 1, 'owned_class_names': ['MCP_SIPI_OWNED']}
        service = SiPiService(tmp_path, native_dispatch=native, snapshot_reader=reader)
        kwargs = {'board_path': data['board_path'], 'topology': ROLES}
        preview = await service.synthesize_and_apply_rules(**kwargs)
        for _ in range(2):
            result = await service.synthesize_and_apply_rules(**kwargs, apply=True,
                expected_snapshot_id=preview['snapshot_id'], expected_preview_id=preview['preview_id'])
            assert not result['ok'] and result['possible_partial_native_edit']
        assert writes[0]['owned_class_names'] == []
        assert writes[1]['owned_class_names'] == ['MCP_SIPI_OWNED']
    asyncio.run(run())


def test_four_public_si_pi_contracts_validate():
    import jsonschema
    tools = list_tools()
    assert len(tools) == 4 and len({t['name'] for t in tools}) == 4
    for tool in tools:
        jsonschema.Draft202012Validator.check_schema(tool['inputSchema'])


def test_classification_partitions_real_pad_bindings_with_existing_domain_modules(tmp_path):
    async def run():
        data = snapshot(tmp_path)
        data['pads'] = [{'id': ref + '.' + str(i), 'component': ref, 'pad_name': str(i),
            'net_name': net, 'layer': 'Top', 'x_mm': i, 'y_mm': 0, 'width_mm': .4, 'height_mm': .4}
            for i, (ref, net) in enumerate([('U1', 'A'), ('U2', 'SW'), ('ADC1', 'A'), ('ADC1', 'SW')])]
        result = await SiPiService(tmp_path).classify_and_partition(snapshot=data, topology=ROLES)
        partition = result['classification']['partition']
        assert partition['domains'] == {'analog': ('U1',), 'digital': ('U2',), 'boundary': ('ADC1',)}
        assert set(partition['connectivity_groups']['group_of']) == {'U1', 'U2', 'ADC1'}
        assert not partition['placement_applied'] and not partition['reference_plane_split_recommended']
    asyncio.run(run())


def test_dsn_provenance_hashes_the_bytes_actually_parsed(tmp_path, monkeypatch):
    import hashlib
    import suite_routing_constraints
    dsn = tmp_path / 'original.dsn'
    original = b'(pcb board (resolution mil 1000) (network (net A) (net SW) (net GND)))'
    dsn.write_bytes(original)
    real = suite_routing_constraints.constrain_dsn
    def mutating_reader(text, context, capabilities):
        dsn.write_text('(pcb externally_changed)')
        return real(text, context, capabilities)
    monkeypatch.setattr(suite_routing_constraints, 'constrain_dsn', mutating_reader)
    async def run():
        data = snapshot(tmp_path)
        data.update(tracks=[], regions=[])
        result = await SiPiService(tmp_path).synthesize_and_apply_rules(snapshot=data,
            topology={'net_roles': {'A': 'control', 'SW': 'signal', 'GND': 'ground'}}, dsn_path=str(dsn))
        assert result['dsn']['original_sha256'] == hashlib.sha256(original).hexdigest()
        assert result['dsn']['original_sha256'] != hashlib.sha256(dsn.read_bytes()).hexdigest()
        assert not result['board_modified'] and result['preview_id']
    asyncio.run(run())
