"""Differential routing widths belong to the differential rule, independently of gaps."""
import asyncio
from pathlib import Path
import sys

import pytest
from mcp.server.fastmcp.exceptions import ToolError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'eda-agent' / 'src'))
from eda_agent.tools import pcb
from eda_agent.tools.registry import ToolRegistry
from eda_agent.design.copper_rules import synthesize_copper_rules
from suite_si_pi import prepare_native_rule_batch


def test_public_decimal_pair_widths_preserve_native_readback(monkeypatch):
    calls = []
    native = {'success': True, 'data': {'created': True, 'gap_verified': True,
              'pair_width_verified': True, 'pair_width_min_mils': 4.5,
              'pair_width_max_mils': 5.1, 'pair_width_preferred_mils': 4.728}}
    class Bridge:
        async def send_command_async(self, name, params):
            calls.append((name, params))
            return native
    monkeypatch.setattr(pcb, 'get_bridge', lambda: Bridge())
    registry = ToolRegistry()
    pcb.register_pcb_tools(registry)
    result = asyncio.run(registry.call_tool('pcb_create_design_rule', {
        'name': 'USB', 'rule_type': 'differential_pairs', 'value': 6.125,
        'pair_width_min_mils': 4.5, 'pair_width_max_mils': 5.1,
        'pair_width_preferred_mils': 4.728}))
    assert result == native
    assert calls == [('pcb.create_design_rule', {'name': 'USB', 'rule_type': 'differential_pairs',
        'value': '6.125', 'net_scope': 'different_nets', 'pair_width_min_mils': '4.5',
        'pair_width_max_mils': '5.1', 'pair_width_preferred_mils': '4.728'})]


def test_legacy_gap_only_keeps_native_unverified_width_receipt(monkeypatch):
    class Bridge:
        async def send_command_async(self, name, params):
            assert not any(key.startswith('pair_width') for key in params)
            return {'created': True, 'gap_verified': True, 'pair_width_verified': False}
    monkeypatch.setattr(pcb, 'get_bridge', lambda: Bridge())
    registry = ToolRegistry()
    pcb.register_pcb_tools(registry)
    result = asyncio.run(registry.call_tool('pcb_create_design_rule', {'name': 'Legacy', 'rule_type': 'differential_pairs'}))
    assert result['gap_verified'] and not result['pair_width_verified']


@pytest.mark.parametrize('values', [
    {'pair_width_min_mils': 4.5},
    {'pair_width_min_mils': 5, 'pair_width_max_mils': 6, 'pair_width_preferred_mils': 4},
    {'pair_width_min_mils': 0, 'pair_width_max_mils': 6, 'pair_width_preferred_mils': 5},
    {'pair_width_min_mils': 4, 'pair_width_max_mils': float('inf'), 'pair_width_preferred_mils': 5},
    {'pair_width_min_mils': 4, 'pair_width_max_mils': 200001, 'pair_width_preferred_mils': 5},
])
def test_invalid_pair_widths_fail_before_dispatch(tmp_path, monkeypatch, values):
    def forbidden():
        pytest.fail('Invalid width reached the native bridge')
    monkeypatch.setattr(pcb, 'get_bridge', forbidden)
    registry = ToolRegistry()
    pcb.register_pcb_tools(registry)
    with pytest.raises(ToolError, match='pair width|Pair width'):
        asyncio.run(registry.call_tool('pcb_create_design_rule', {'name': 'Bad', 'rule_type': 'differential_pairs', **values}))
    with pytest.raises(ValueError):
        prepare_native_rule_batch(str(tmp_path / 'board.PcbDoc'), [], [
            {'name': 'Bad', 'rule_type': 'differential_pairs', 'value': 6, **values}])


def test_synthesized_explicit_width_is_applied_to_pair_rule(tmp_path):
    result = synthesize_copper_rules({}, {'net_classes': {'P': ['CONTROL'], 'N': ['CONTROL']},
        'constraints': {net: {'required_width_mils': 7.125} for net in ('P', 'N')},
        'differential_pairs': [{'name': 'USB', 'nets': ['P', 'N'], 'gap_mils': 6}]})
    assert result['coverage']['complete'], result['coverage']
    rule = next(r for r in result['rules'] if r['rule_type'] == 'differential_pairs')
    assert all(rule[key] == 7.125 for key in ('pair_width_min_mils', 'pair_width_max_mils', 'pair_width_preferred_mils'))
    packed = prepare_native_rule_batch(str(tmp_path / 'board.PcbDoc'), [], [rule])
    assert packed['rule_0']['pair_width_preferred_mils'] == 7.125


def test_generic_gap_only_is_retained_with_incomplete_width_coverage():
    result = synthesize_copper_rules({}, {'net_classes': {'P': ['CONTROL'], 'N': ['CONTROL']},
        'differential_pairs': [{'name': 'USB', 'nets': ['P', 'N'], 'gap_mils': 6}]})
    assert any(r['rule_type'] == 'differential_pairs' for r in result['rules'])
    assert not result['coverage']['complete']
    assert any(item['constraint'] == 'differential_pair_width' for item in result['coverage']['unsupported'])
