"""Copper-aware orchestration of the existing EDA design modules.

SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time

_SOURCE = Path(__file__).resolve().parent / 'eda-agent' / 'src'
if _SOURCE.is_dir() and str(_SOURCE) not in sys.path:
    sys.path.insert(0, str(_SOURCE))

from eda_agent.design.copper_snapshot import validate_snapshot, content_fingerprint, same_board_path


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('utf-8')


def _path(value):
    path = Path(value)
    if not path.is_absolute() or path.suffix.casefold() != '.pcbdoc':
        raise ValueError('An absolute .PcbDoc board_path is required')
    # A loaded Altium document is bound to its logical path. Resolving a
    # Windows workspace junction can change D:\\a into another volume/path
    # and falsely reject the same caller-supplied snapshot (also seen in CI).
    return os.path.abspath(path)


def _editor_absent():
    if os.name != 'nt':
        return True
    try:
        import psutil
        return not any((p.info.get('name') or '').casefold() in
                       {'x2.exe', 'dxp.exe', 'altiumdesigner.exe'}
                       for p in psutil.process_iter(['name']))
    except Exception:
        return False


def prepare_native_rule_batch(board_path, net_classes, rules, differential_pairs=None):
    """Validate the entire batch before publishing any native mutation."""
    from coffeenmusic.server.batch_safety import validate_legacy_json_text
    target = _path(board_path)
    if len(net_classes) > 500 or len(rules) > 2000 or len(differential_pairs or []) > 500:
        raise ValueError('Copper rule batch exceeds its bounded native size')
    packed = {'board_path': target, 'class_count': len(net_classes), 'rule_count': len(rules),
              'pair_count': len(differential_pairs or [])}
    names, class_names, pair_names = set(), set(), set()
    for i, group in enumerate(net_classes):
        name, nets = group.get('name'), group.get('net_names')
        if not isinstance(name, str) or not name or not isinstance(nets, list) or not nets:
            raise ValueError('Net classes require a name and nonempty net_names')
        if not all(isinstance(net, str) and net for net in nets):
            raise ValueError('Net class members must be net names')
        if name in class_names or len(nets) != len(set(nets)):
            raise ValueError('Net class names and their members must be unique')
        class_names.add(name)
        csv = ','.join('"' + net.replace('"', '""') + '"' for net in nets)
        packed['class_' + str(i)] = {'name': name, 'nets': csv}
    for i, raw in enumerate(rules):
        rule = copy.deepcopy(raw)
        if rule.get('rule_type') not in {'clearance', 'width', 'via_size', 'differential_pairs', 'matched_lengths'}:
            raise ValueError('Unsupported native copper rule type')
        if not isinstance(rule.get('name'), str) or not rule['name'] or rule['name'] in names:
            raise ValueError('Rule names must be nonempty and unique')
        names.add(rule['name'])
        for key in ('value', 'max_value', 'favored_value', 'max_uncoupled_length'):
            value = rule.get(key)
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 200000):
                raise ValueError('Rule dimensions must be finite positive mils within native coordinate range')
        if 'value' not in rule:
            raise ValueError('Native rule value is required')
        if rule.get('max_value', rule['value']) < rule['value']:
            raise ValueError('Maximum rule dimension is below its minimum')
        if rule['rule_type'] in {'width', 'differential_pairs'} and not rule['value'] <= rule.get('favored_value', rule['value']) <= rule.get('max_value', rule['value'] * 5):
            raise ValueError('Preferred rule dimension is outside minimum/maximum')
        pair_width_keys = ('pair_width_min_mils', 'pair_width_max_mils', 'pair_width_preferred_mils')
        if any(key in rule for key in pair_width_keys):
            pair_widths = [rule.get(key) for key in pair_width_keys]
            if rule['rule_type'] != 'differential_pairs' or any(
                type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 200000
                for value in pair_widths
            ):
                raise ValueError('Differential pair widths require all three finite positive mil values within native coordinate range')
            if not pair_widths[0] <= pair_widths[2] <= pair_widths[1]:
                raise ValueError('Differential pair width requires min <= preferred <= max')
        for key in ('scope', 'scope2'):
            if key in rule and (not isinstance(rule[key], str) or not rule[key].strip()):
                raise ValueError('Rule scopes must be nonempty query strings')
        rule['managed_by'] = 'mcp-altium-suite:copper-si-pi:v1'
        packed['rule_' + str(i)] = rule
    for i, pair in enumerate(differential_pairs or []):
        members = pair.get('nets', [])
        if not isinstance(pair.get('name'), str) or not pair['name'] or len(members) != 2 or not all(isinstance(n, str) and n for n in members):
            raise ValueError('Differential pairs require a name and two net names')
        if pair['name'] in pair_names or members[0] == members[1]:
            raise ValueError('Differential pair names must be unique and members distinct')
        pair_names.add(pair['name'])
        packed['pair_' + str(i)] = {'name': pair['name'], 'positive_net': members[0], 'negative_net': members[1]}
    validate_legacy_json_text(packed)
    return packed


@asynccontextmanager
async def _unlocked():
    yield


class SiPiService:
    def __init__(self, data_dir=None, *, native_dispatch=None, native_sequence=None,
                 snapshot_reader=None):
        self.data_dir = Path(data_dir or Path.home() / '.altium-suite').resolve() / 'si-pi'
        self.native_dispatch = native_dispatch
        self.native_sequence = native_sequence or _unlocked
        self.snapshot_reader = snapshot_reader

    @staticmethod
    def list_tools():
        return list_tools()

    def _save(self, kind, value):
        from suite_checkpoints import _atomic
        data = _json(value)
        if len(data) > 64 * 1024 * 1024:
            raise ValueError('SI/PI evidence exceeds 64 MiB')
        identity = hashlib.sha256(data).hexdigest()
        _atomic(self.data_dir / kind / (identity + '.json'), data)
        return identity

    def _load(self, kind, identity):
        if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{64}', identity):
            raise ValueError('A valid SI/PI evidence identifier is required')
        path = self.data_dir / kind / (identity + '.json')
        if path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError('SI/PI evidence exceeds 64 MiB')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != identity:
            raise ValueError('SI/PI evidence checksum mismatch')
        return json.loads(data)

    async def _native(self, name, arguments):
        if self.native_dispatch is None:
            raise ValueError('A connected Altium backend is required')
        result = await self.native_dispatch(name, arguments)
        if hasattr(result, 'isError'):
            if result.isError:
                raise ValueError('; '.join(c.text for c in result.content if c.type == 'text'))
            if result.structuredContent is not None:
                result = result.structuredContent
            else:
                result = json.loads(next(c.text for c in result.content if c.type == 'text'))
        if not isinstance(result, dict):
            raise ValueError('Native copper response must be an object')
        return result

    async def _snapshot(self, board_path=None, snapshot=None, source='auto'):
        if source not in {'auto', 'file', 'live'}:
            raise ValueError('source must be auto, file or live')
        if snapshot is not None:
            value = copy.deepcopy(snapshot)
            board_path = _path(board_path or value.get('board_path', ''))
            validate_snapshot(value, expected_board_path=board_path)
            # Caller-provided evidence is suitable for analysis, never proof
            # that the current editor can safely be changed.
            trusted_live = False
        else:
            board_path = _path(board_path)
            from suite_reads import capture_copper_snapshot
            reader = self.snapshot_reader or capture_copper_snapshot
            async def native_read(command, arguments):
                if command != 'pcb.get_copper_snapshot':
                    raise ValueError('Unexpected copper reader command')
                return await self._native('_suite_copper_snapshot', arguments)
            value = await reader(board_path, native_reader=native_read if self.native_dispatch else None,
                prefer_live=source != 'file',
                open_in_memory=False if source == 'file' or _editor_absent() else None)
            validate_snapshot(value, expected_board_path=board_path)
            if source == 'live' and value.get('source') != 'live':
                raise ValueError('A live Altium copper snapshot was not available')
            trusted_live = value.get('source') == 'live' and self.native_dispatch is not None
        record = {'snapshot': value, 'trusted_live': trusted_live,
                  'fingerprint': content_fingerprint(value), 'captured_at': time.time()}
        identity = await asyncio.to_thread(self._save, 'snapshots', record)
        return identity, record

    async def _intent(self, snapshot, plan=None, topology=None, net_constraints=None):
        from eda_agent.design.copper_audit import classify_copper_intent
        topology = copy.deepcopy(topology or {})
        topology['board_snapshot'] = snapshot
        intent = await asyncio.to_thread(classify_copper_intent, plan, topology)
        captured = set(snapshot['nets'])
        references = set(intent.get('net_classes', {})) | set(intent.get('constraints', {}))
        for motif in intent.get('motifs', []):
            references.update(motif.get('nets', []))
            references.update(motif[k] for k in ('net_name', 'ground_net') if motif.get(k))
        for pair in intent.get('differential_pairs', []):
            references.update(pair.get('nets', []))
        if references - captured:
            raise ValueError('Electrical intent references uncaptured net names: ' + ', '.join(sorted(references - captured)))
        for net, constraints in (net_constraints or {}).items():
            if net not in snapshot['nets'] or not isinstance(constraints, dict):
                raise ValueError('Explicit constraints must target captured net names')
            _json(constraints)
            intent.setdefault('constraints', {}).setdefault(net, {}).update(copy.deepcopy(constraints))
        # Reuse the existing mixed-signal and min-cut modules on actual PCB
        # pad/net bindings. Suggested rooms do not move components or split GND.
        from types import SimpleNamespace
        from dataclasses import asdict
        from eda_agent.design.mixed_signal import classify_domains
        from eda_agent.design.partition import partition_netlist
        physical_nets, signal_members, refs = [], [], set()
        for net, tags in sorted(intent['net_classes'].items()):
            pads = [p for p in snapshot['pads'] if p.get('net_name') == net and p.get('component')]
            pins = [SimpleNamespace(refdes=p['component'], pin=p.get('pad_name', '')) for p in pads]
            members = sorted({p.refdes for p in pins})
            refs.update(members)
            role = 'analog_sensitive' if 'SENSITIVE_ANALOG' in tags else 'switch' if set(tags) & {
                'AGGRESSIVE_SWITCHING', 'HIGH_SPEED_SINGLE', 'HIGH_SPEED_DIFF'} else ''
            physical_nets.append(SimpleNamespace(role=role, pins=pins))
            if not set(tags) & {'POWER', 'GROUND', 'HIGH_CURRENT_POWER'}:
                signal_members.append(members)
        groups = (await asyncio.to_thread(partition_netlist, sorted(refs), signal_members)
                  if len(refs) <= 64 else {'available': False, 'reason': 'min-cut interactive budget is 64 components; electrical domains remain available'})
        intent['partition'] = {
            'domains': asdict(classify_domains(SimpleNamespace(nets=physical_nets))),
            'connectivity_groups': groups,
            'basis': 'captured PCB pad/net bindings and declared electrical roles',
            'placement_applied': False, 'reference_plane_split_recommended': False}
        return intent

    def _ownership_path(self, board_path):
        import ntpath
        key = hashlib.sha256(ntpath.normcase(ntpath.normpath(board_path)).encode('utf-8')).hexdigest()
        return self.data_dir / 'ownership' / (key + '.json')

    def _load_ownership(self, board_path):
        path = self._ownership_path(board_path)
        if not path.exists():
            return []
        if path.stat().st_size > 1024 * 1024:
            raise ValueError('Invalid SI/PI ownership manifest')
        manifest = json.loads(path.read_bytes())
        if not same_board_path(manifest.get('board_path', ''), board_path):
            raise ValueError('SI/PI ownership manifest targets another board')
        names = manifest.get('owned_class_names')
        if not isinstance(names, list) or not all(isinstance(n, str) and n.startswith('MCP_SIPI_') for n in names):
            raise ValueError('Invalid SI/PI class ownership manifest')
        return names

    def _save_ownership(self, board_path, names):
        from suite_checkpoints import _atomic
        if not isinstance(names, list) or not all(isinstance(n, str) and n.startswith('MCP_SIPI_') for n in names):
            raise ValueError('Native response contains invalid owned classes')
        _atomic(self._ownership_path(board_path), _json({'board_path': board_path, 'owned_class_names': sorted(set(names))}))

    async def classify_and_partition(self, board_path=None, *, snapshot=None, plan=None,
                                     topology=None, net_constraints=None, source='auto'):
        identity, record = await self._snapshot(board_path, snapshot, source)
        intent = await self._intent(record['snapshot'], plan, topology, net_constraints)
        return {'ok': True, 'snapshot_id': identity, 'classification': intent,
                'snapshot_complete': record['snapshot']['complete'],
                'missing': record['snapshot']['missing'], 'source': record['snapshot']['source'],
                'board_modified': False}

    async def audit_copper(self, board_path=None, *, snapshot=None, plan=None,
                           topology=None, net_constraints=None, source='auto', policy=None, options=None):
        from eda_agent.design.copper_audit import audit_board_copper
        from eda_agent.design.rule_synthesis import synthesize_copper_rules
        identity, record = await self._snapshot(board_path, snapshot, source)
        intent = await self._intent(record['snapshot'], plan, topology, net_constraints)
        synthesis = await asyncio.to_thread(synthesize_copper_rules, record['snapshot'], intent, options)
        intent['constraints'] = synthesis['constraints']
        intent['clearance_matrix'] = synthesis['clearance_matrix']
        intent['differential_pairs'] = synthesis['differential_pairs']
        intent.setdefault('unknowns', []).extend('Physics qualification: ' + str(item)
            for item in synthesis.get('coverage', {}).get('unsupported', []))
        audit = await asyncio.to_thread(audit_board_copper, record['snapshot'], intent, policy=policy)
        report = {'snapshot_id': identity, 'board_path': record['snapshot']['board_path'],
                  'fingerprint': record['fingerprint'], 'classification': intent,
                  'audit': audit, 'created_at': time.time()}
        report_id = await asyncio.to_thread(self._save, 'reports', report)
        return {'ok': True, 'report_id': report_id, 'snapshot_id': identity, **audit,
                'source': record['snapshot']['source'], 'board_modified': False}

    async def synthesize_and_apply_rules(self, board_path=None, *, snapshot=None,
            plan=None, topology=None, net_constraints=None, source='auto', options=None,
            apply=False, expected_snapshot_id=None, expected_preview_id=None, allow_partial=False, dsn_path=None):
        from eda_agent.design.rule_synthesis import synthesize_copper_rules
        async with self.native_sequence():
            identity, record = await self._snapshot(board_path, snapshot, source)
            intent = await self._intent(record['snapshot'], plan, topology, net_constraints)
            synthesis = await asyncio.to_thread(synthesize_copper_rules, record['snapshot'], intent, options)
            result = {'ok': True, 'snapshot_id': identity, 'synthesis': synthesis,
                      'applied': False, 'board_modified': False}
            if dsn_path is not None:
                # DSN writing is shared with the router's validated parser.
                from suite_routing_constraints import constrain_dsn
                from suite_autoroute import _file, MAX_DESIGN_BYTES
                original = _file(dsn_path, '.dsn')
                # Hash exactly the bytes parsed, even if the source changes later.
                with original.open('rb') as stream:
                    original_bytes = stream.read(MAX_DESIGN_BYTES + 1)
                if len(original_bytes) > MAX_DESIGN_BYTES:
                    raise ValueError('Routing design exceeds the 128 MiB limit')
                route_context = {'snapshot': record['snapshot'], 'intent': synthesis}
                for key in ('layer_map', 'via_styles'):
                    if options and key in options:
                        route_context[key] = copy.deepcopy(options[key])
                text, evidence = await asyncio.to_thread(constrain_dsn, original_bytes.decode('utf-8-sig'),
                    route_context, [])
                from suite_checkpoints import _atomic
                dest = self.data_dir / 'dsn' / (hashlib.sha256(text.encode()).hexdigest() + '.dsn')
                await asyncio.to_thread(_atomic, dest, text.encode('utf-8'))
                result['dsn'] = {'path': str(dest), 'original_sha256': hashlib.sha256(original_bytes).hexdigest(),
                                 'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(), 'evidence': evidence}
            # A copper identity alone cannot authorize a changed electrical plan.
            reviewed = {'fingerprint': record['fingerprint'], 'intent': intent, 'synthesis': synthesis,
                        'options': options, 'allow_partial': allow_partial,
                        'dsn': {k: v for k, v in result.get('dsn', {}).items() if k != 'path'}}
            review_hash = hashlib.sha256(_json(reviewed)).hexdigest()
            if not apply:
                result['preview_id'] = await asyncio.to_thread(self._save, 'previews', {
                    'snapshot_id': identity, 'review_hash': review_hash, 'reviewed': reviewed})
                if dsn_path is not None:
                    result['routing_context'] = {'snapshot_id': identity, 'preview_id': result['preview_id']}
                    result['routing_input_path'] = str(original)
                    result['routing_input_note'] = 'Route the unchanged original DSN with routing_context; the router reapplies constraints and audits SES.'
                return result
            if snapshot is not None or not record['trusted_live']:
                raise ValueError('Rule application requires a fresh live snapshot, not supplied or disk evidence')
            if not expected_snapshot_id:
                raise ValueError('Preview first and supply expected_snapshot_id before applying rules')
            previous = await asyncio.to_thread(self._load, 'snapshots', expected_snapshot_id)
            if not previous.get('trusted_live') or previous['fingerprint'] != record['fingerprint']:
                raise ValueError('Copper changed since the live preview; recompute the rules')
            if not expected_preview_id:
                raise ValueError('Supply expected_preview_id from the reviewed rule preview')
            preview = await asyncio.to_thread(self._load, 'previews', expected_preview_id)
            if preview.get('snapshot_id') != expected_snapshot_id or preview.get('review_hash') != review_hash:
                raise ValueError('Rules or options changed since the reviewed preview; preview again')
            complete = record['snapshot']['complete'] and synthesis.get('coverage', {}).get('complete') is True
            if not complete and not allow_partial:
                raise ValueError('Incomplete copper or physics evidence; review coverage before allowing partial rule application')
            proof_key = ('native_snapshot_token' if record['snapshot'].get('native_snapshot_token')
                         else 'native_snapshot_json')
            native_snapshot = record['snapshot'].get(proof_key)
            if not isinstance(native_snapshot, str) or not native_snapshot:
                raise ValueError('Native copper freshness proof unavailable; rule application refused')
            for pair in synthesis.get('differential_pairs', []):
                if set(pair.get('nets', [])) - set(record['snapshot']['nets']):
                    raise ValueError('Rule pair references uncaptured net names')
            owned_names = await asyncio.to_thread(self._load_ownership, record['snapshot']['board_path'])
            native = await self._native('_suite_copper_apply_rules', {
                'board_path': record['snapshot']['board_path'],
                ('expected_native_snapshot_token' if proof_key == 'native_snapshot_token'
                 else 'expected_native_snapshot'): native_snapshot, 'owned_class_names': owned_names,
                'net_classes': [{'name': name, 'net_names': members} for name, members in
                                synthesis.get('class_memberships', {}).items() if members],
                'rules': synthesis.get('rules', []),
                'differential_pairs': synthesis.get('differential_pairs', [])})
            if 'owned_class_names' in native:
                await asyncio.to_thread(self._save_ownership, record['snapshot']['board_path'], native['owned_class_names'])
            result.update(applied=native.get('success') is True,
                          board_modified=native.get('applied_count', 0) > 0 or native.get('possible_partial_native_edit') is True,
                          native=native, native_rollback_available=False)
            if native.get('success') is not True:
                result.update(ok=False, possible_partial_native_edit=True, automatic_retry=False)
            return result

    def resolve_routing_context(self, handle, original_input_sha256):
        """Resolve private reviewed evidence without sending copper through the LLM."""
        if not isinstance(handle, dict) or set(handle) != {'snapshot_id', 'preview_id'}:
            raise ValueError('Routing evidence requires exactly snapshot_id and preview_id')
        preview = self._load('previews', handle['preview_id'])
        record = self._load('snapshots', handle['snapshot_id'])
        if preview.get('snapshot_id') != handle['snapshot_id']:
            raise ValueError('Routing snapshot does not belong to the reviewed preview')
        reviewed = preview['reviewed']
        if hashlib.sha256(_json(reviewed)).hexdigest() != preview.get('review_hash'):
            raise ValueError('Routing preview review checksum mismatch')
        snapshot, synthesis = record['snapshot'], reviewed['synthesis']
        validate_snapshot(snapshot)
        if record.get('fingerprint') != content_fingerprint(snapshot) or reviewed.get('fingerprint') != record['fingerprint']:
            raise ValueError('Routing snapshot geometry does not match the reviewed preview')
        if snapshot.get('complete') is not True or synthesis.get('coverage', {}).get('complete') is not True:
            raise ValueError('Routing evidence requires complete copper and physics coverage')
        expected = reviewed.get('dsn', {}).get('original_sha256')
        if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
            raise ValueError('Routing preview must include a reviewed source DSN')
        if expected != original_input_sha256:
            raise ValueError('DSN bytes changed since the reviewed preview; route the unchanged original input')
        context = {'snapshot': copy.deepcopy(snapshot), 'intent': copy.deepcopy(synthesis)}
        for key in ('layer_map', 'via_styles'):
            if key in (reviewed.get('options') or {}):
                context[key] = copy.deepcopy(reviewed['options'][key])
        return context

    async def focus_violation(self, report_id, violation_id, *, radius_mm=2.0):
        if type(radius_mm) not in (int, float) or not math.isfinite(radius_mm) or not 0 < radius_mm <= 100:
            raise ValueError('radius_mm must be positive and at most 100 mm')
        report = await asyncio.to_thread(self._load, 'reports', report_id)
        violation = next((v for v in report['audit']['violations'] if v['id'] == violation_id), None)
        if violation is None:
            raise ValueError('Violation does not belong to this report')
        async with self.native_sequence():
            _, current = await self._snapshot(report['board_path'], source='live')
            if current['fingerprint'] != report['fingerprint']:
                raise ValueError('The copper report is stale; audit again before cross-probing')
            return await self._native('_suite_copper_focus', {
                'board_path': report['board_path'], 'x_mm': violation['x_mm'], 'y_mm': violation['y_mm'],
                'board_identity': current['snapshot'].get('board_identity', ''),
                'layer': violation['layer'], 'net_names': [violation['net_name']], 'radius_mm': radius_mm})

    async def call_tool(self, name, arguments=None):
        if name not in {t['name'] for t in list_tools()}:
            raise ValueError('Unknown SI/PI tool')
        return await getattr(self, name)(**(arguments or {}))

    call_tool_async = call_tool


def list_tools():
    common = {'board_path': {'type': 'string'}, 'snapshot': {'type': 'object'},
              'plan': {'type': 'object'}, 'topology': {'type': 'object'},
              'net_constraints': {'type': 'object'},
              'source': {'type': 'string', 'enum': ['auto', 'file', 'live'], 'default': 'auto'}}
    definitions = [
        ('classify_and_partition', 'Classify physical net intent using existing motifs and mixed-signal/HSD modules; unresolved roles remain unknown.', {}, False),
        ('audit_copper', 'Audit real 2D PCB copper, return physical violation coordinates and explicit coverage gaps. File mode analyzes saved bytes, not unsaved edits.', {'policy': {'type': 'object'}, 'options': {'type': 'object'}}, False),
        ('synthesize_and_apply_rules', 'Preview stackup/current-aware rules or explicitly apply them after a fresh live preview. Missing qualified physics data remains unsupported.', {
            'options': {'type': 'object'}, 'apply': {'type': 'boolean', 'default': False},
            'expected_snapshot_id': {'type': 'string'}, 'allow_partial': {'type': 'boolean', 'default': False},
            'expected_preview_id': {'type': 'string'},
            'dsn_path': {'type': 'string'}}, True),
    ]
    tools = []
    for name, description, extra, writes in definitions:
        tools.append({'name': name, 'description': description,
            'inputSchema': {'type': 'object', 'properties': {**common, **extra},
                            'anyOf': [{'required': ['board_path']}, {'required': ['snapshot']}],
                            'additionalProperties': False},
            'annotations': {'readOnlyHint': not writes, 'destructiveHint': writes}})
    tools.append({'name': 'focus_violation', 'description': 'Cross-probe a stored, still-current copper violation in Altium; zoom and select its net without editing copper.',
        'inputSchema': {'type': 'object', 'properties': {'report_id': {'type': 'string'},
            'violation_id': {'type': 'string'}, 'radius_mm': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 100, 'default': 2}},
            'required': ['report_id', 'violation_id'], 'additionalProperties': False},
        'annotations': {'readOnlyHint': True}})
    return tools
