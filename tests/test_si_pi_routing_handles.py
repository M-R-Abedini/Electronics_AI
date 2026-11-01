"""Reviewed SI/PI evidence reaches routing without LLM-sized copper payloads."""
import asyncio
import copy
import hashlib

import pytest

from suite_si_pi import SiPiService
from suite_autoroute import AutorouteService
from eda_agent.design.copper_snapshot import empty_snapshot


DSN = '(pcb board (resolution mil 1000) (network (net CTRL)))'
SES = '(session board (routes (resolution mil 1000) (network_out (net CTRL (wire (path top 6000 0 0 100000 0))))))'


def preview(tmp_path):
    service = SiPiService(tmp_path / 'state')
    snapshot = empty_snapshot(str(tmp_path / 'Board.PcbDoc'), source='file')
    snapshot.update(complete=True, missing=[], nets=['CTRL'],
        coverage={key: 'complete' for key in ('tracks', 'arcs', 'vias', 'pads', 'regions', 'layers', 'nets', 'components', 'keepouts', 'net_ties')},
        layers=[{'name': 'top', 'kind': 'signal', 'order': 0, 'thickness_mm': .035}],
        tracks=[{'id': 'old', 'net_name': 'CTRL', 'layer': 'top', 'start': [0, 0],
                 'end': [2.54, 0], 'width_mm': .1524}])
    source = tmp_path / 'original.dsn'
    source.write_text(DSN, encoding='utf-8')
    result = asyncio.run(service.synthesize_and_apply_rules(snapshot=snapshot,
        topology={'net_roles': {'CTRL': 'control'}}, dsn_path=str(source),
        options={'layer_map': {'top': 'top'}, 'via_styles': {}}))
    return service, source, result


def test_review_handle_routes_original_and_runs_production_postroute_audit(tmp_path):
    service, source, result = preview(tmp_path)
    original = source.read_bytes()
    assert result['routing_input_path'] == str(source)
    assert set(result['routing_context']) == {'snapshot_id', 'preview_id'}
    class Router:
        def route(self, input_path, output, **kwargs):
            assert '(class ' in input_path.read_text()
            output.write_text(SES)
            return {'session_path': str(output)}
    routed = AutorouteService(tmp_path / 'jobs', provider=Router(),
        routing_context_resolver=service.resolve_routing_context).route_dsn(str(source),
            constraints_context=result['routing_context'])
    assert routed['copper_audit']['status'] == 'clean'
    assert routed['copper_audit']['coverage_complete'] is True
    assert source.read_bytes() == original


def test_review_handles_reject_dsn_changes_wrong_snapshot_partial_and_corruption(tmp_path):
    service, source, result = preview(tmp_path)
    handle = result['routing_context']
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    resolved = service.resolve_routing_context(handle, digest)
    assert resolved['layer_map'] == {'top': 'top'} and resolved['via_styles'] == {}
    resolved['layer_map'].clear()
    resolved['intent']['constraints'].clear()
    assert service.resolve_routing_context(handle, digest)['intent']['constraints']
    assert service.resolve_routing_context(handle, digest)['layer_map'] == {'top': 'top'}
    with pytest.raises(ValueError, match='DSN bytes changed'):
        service.resolve_routing_context(handle, '0' * 64)
    with pytest.raises(ValueError, match='DSN bytes changed'):
        service.resolve_routing_context(handle, result['dsn']['sha256'])
    other = service._load('snapshots', handle['snapshot_id'])
    other['captured_at'] += 1
    wrong = service._save('snapshots', other)
    with pytest.raises(ValueError, match='does not belong'):
        service.resolve_routing_context(dict(handle, snapshot_id=wrong), digest)
    stored = service._load('previews', handle['preview_id'])
    partial = copy.deepcopy(stored)
    partial['reviewed']['synthesis']['coverage']['complete'] = False
    from suite_si_pi import _json
    partial['review_hash'] = hashlib.sha256(_json(partial['reviewed'])).hexdigest()
    partial_id = service._save('previews', partial)
    with pytest.raises(ValueError, match='complete copper and physics'):
        service.resolve_routing_context(dict(handle, preview_id=partial_id), digest)
    with pytest.raises(ValueError, match='exactly'):
        service.resolve_routing_context(dict(handle, snapshot={}), digest)
    (service.data_dir / 'previews' / (handle['preview_id'] + '.json')).write_text('{}')
    with pytest.raises(ValueError, match='checksum'):
        service.resolve_routing_context(handle, digest)


def test_router_verifies_isolated_input_hash_before_provider_execution(tmp_path):
    service, source, result = preview(tmp_path)
    class Router:
        def route(self, *args, **kwargs):
            raise AssertionError('A stale review must never reach the router')
    source.write_text(DSN + '\n')
    with pytest.raises(ValueError, match='DSN bytes changed'):
        AutorouteService(tmp_path / 'jobs', provider=Router(),
            routing_context_resolver=service.resolve_routing_context).route_dsn(str(source),
                constraints_context=result['routing_context'])
    assert not list((tmp_path / 'jobs').rglob('result.ses'))
    with pytest.raises(ValueError, match='resolver is not connected'):
        AutorouteService(tmp_path / 'other', provider=Router()).route_dsn(str(source),
            constraints_context=result['routing_context'])
