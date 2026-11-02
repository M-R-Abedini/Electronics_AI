"""Evidence-bound placement over the existing copper reader and PCB solvers.

SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import asyncio
import copy
import contextvars
import hashlib
import json
from pathlib import Path
import re
import time

from suite_si_pi import SiPiService, _json, _path
from suite_checkpoints import _atomic, digest

POLICY_VERSION = 'placement-evidence-v1'


def capture_digest(snapshot):
    """Bind live rules, locks and geometry; exclude only capture transport data."""
    return digest({k: v for k, v in snapshot.items() if k not in {
        'native_snapshot_token', 'native_snapshot_json', 'capture_metrics', 'provenance'}})


def context_from_snapshot(snapshot, snapshot_id, models, fabrication, assembly):
    """Use explicit pad ownership. A native AABB never becomes a courtyard."""
    components, references = [], {}
    allowed_model = {'courtyard_local', 'courtyard_source', 'body_local', 'height_mm',
                     'through_hole', 'allowed_rotations', 'allowed_sides', 'overhang'}
    for row in snapshot['components']:
        identity, ref = str(row['id']), row.get('designator', '')
        if not ref or ref in references:
            raise ValueError('Placement requires unique, nonblank captured component references')
        references[ref] = identity
        model = copy.deepcopy(models.get(identity, models.get(ref, {})))
        if set(model) - allowed_model:
            raise ValueError('Package geometry cannot override captured identity, pose or lock state')
        layer = row.get('layer', '')
        side = row.get('side') or ('bottom' if 'bottom' in layer.lower() else
                                   'top' if 'top' in layer.lower() else 'unknown')
        components.append(dict(model, id=identity, reference=ref, x_mm=row['x_mm'], y_mm=row['y_mm'],
            rotation_deg=row.get('rotation_deg', 0), side=side, locked=row.get('locked'),
            fixed=row.get('locked') is not False))
    unknown_models = set(models) - set(references) - set(references.values())
    if unknown_models:
        raise ValueError('Geometry models reference uncaptured components: ' + ', '.join(sorted(unknown_models)))
    pads = []
    for row in snapshot['pads']:
        owner = references.get(row.get('component'))
        if owner is None:
            continue  # Free copper pads remain obstacles, not fictional package pins.
        pads.append(dict(copy.deepcopy(row), component_id=owner))
    cutouts = [copy.deepcopy(row.get('outline', [])) for row in snapshot.get('cutouts', [])
               if isinstance(row, dict) and row.get('outline')]
    result = dict(schema=1, units='mm', board_path=snapshot['board_path'],
        board_identity=snapshot.get('board_identity'), snapshot_id=snapshot_id,
        source=snapshot['source'], document_modified=snapshot.get('document_modified'),
        outline=copy.deepcopy(snapshot.get('board_outline', [])), cutouts=cutouts,
        layers=copy.deepcopy(snapshot['layers']), components=components, pads=pads,
        fabrication_profile=copy.deepcopy(fabrication), assembly_profile=copy.deepcopy(assembly),
        coverage={'outline': 'complete' if snapshot.get('board_outline') else 'unknown',
                  'routing_obstacles': 'complete' if snapshot.get('complete') else 'partial'},
        native_rules=copy.deepcopy(snapshot.get('native_rules')),
        copper_snapshot=copy.deepcopy(snapshot),
        renderer_coverage={'courtyard': 'unavailable', 'pours': 'unavailable',
                           'electrical_verification': False})
    if snapshot.get('origin_mm') is not None:
        result['origin_mm'] = copy.deepcopy(snapshot['origin_mm'])
    return result


def list_tools():
    obj = {'type': 'object'}
    string = {'type': 'string'}
    def tool(name, description, properties, required=()):
        return dict(name=name, description=description, inputSchema=dict(type='object',
            properties=properties, required=list(required), additionalProperties=False))
    return [
        tool('get_contracts', 'Discover the actual placement context, intent and constraint contracts; no CAD call.', {}),
        tool('capture_context', 'Capture an explicit loaded PCB, or register caller-provided offline geometry. Does not save or edit Altium.',
             dict(board_path=string, board_context=obj, package_geometry=obj, fabrication_profile=obj, assembly_profile=obj)),
        tool('plan_placement', 'Reuse seeded PCB solvers, compare baseline and alternatives with hard constraints outside score. No CAD edits.',
             dict(context_id=string, design_intent=obj, constraints={'type': 'array', 'items': obj}, options=obj), ['context_id']),
        tool('validate_plan', 'Re-evaluate the exact stored moves against fresh live geometry and recovery policy; caller stage results cannot pass this gate.',
             dict(plan_id=string), ['plan_id']),
        tool('apply_plan', 'Apply the exact validated plan once, with checkpoint, native freshness and readback; never rerun the optimizer.',
             dict(plan_id=string, validation_id=string, operation_id=string), ['plan_id', 'validation_id', 'operation_id']),
        tool('get_evidence', 'Read an integrity-checked server-created context, plan, gate or receipt. Does not certify its current freshness.',
             dict(evidence_id=string, kind={'enum': ['contexts', 'plans', 'validations', 'receipts']}), ['evidence_id', 'kind']),
        tool('end_managed_scope', 'Abandon a managed placement scope without marking any gate passed. Enables ordinary unqualified editing again.',
             dict(context_id=string, reason=string), ['context_id', 'reason']),
    ]


class LayoutService(SiPiService):
    def __init__(self, data_dir=None, *, native_dispatch=None, native_sequence=None,
                 snapshot_reader=None, checkpoint=None):
        super().__init__(data_dir, native_dispatch=native_dispatch, native_sequence=native_sequence,
                         snapshot_reader=snapshot_reader)
        self.data_dir = Path(data_dir or Path.home() / '.altium-suite').resolve() / 'placement'
        self.checkpoint = checkpoint
        self._lock = asyncio.Lock()
        self._native_ticket = contextvars.ContextVar('placement_native_ticket', default=None)

    list_tools = staticmethod(list_tools)

    async def call_tool_async(self, name, arguments):
        if name == 'get_contracts':
            from eda_agent.design.placement_evidence import contract_schemas
            return dict(policy_version=POLICY_VERSION, schemas=contract_schemas(),
                        native_apply={'side_changes': False, 'transactional_rollback': False,
                                      'recovery': 'saved_disk_bytes_and_original_pose_evidence'})
        if name == 'get_evidence':
            return await asyncio.to_thread(self._load, arguments['kind'], arguments['evidence_id'])
        method = getattr(self, name, None)
        if name not in {t['name'] for t in list_tools()} or method is None:
            raise ValueError('Unknown placement capability')
        async with self._lock:
            return await method(**arguments)

    async def capture_context(self, board_path=None, board_context=None, package_geometry=None,
                              fabrication_profile=None, assembly_profile=None):
        from eda_agent.design.placement_evidence import validate_board_context
        if board_context is not None:
            context = copy.deepcopy(board_context)
            if board_path and _path(board_path) != _path(context['board_path']):
                raise ValueError('Offline context targets another board')
            context['source'] = 'caller_provided'
            validate_board_context(context)
            record = dict(context=context, trusted_live=False, snapshot=None, captured_at=time.time())
        else:
            snapshot_id, raw = await self._snapshot(board_path=board_path, source='live')
            context = context_from_snapshot(raw['snapshot'], snapshot_id, package_geometry or {},
                                            fabrication_profile or {}, assembly_profile or {})
            validate_board_context(context)
            record = dict(context=context, trusted_live=raw['trusted_live'], snapshot=raw['snapshot'],
                capture_digest=capture_digest(raw['snapshot']), models=package_geometry or {},
                fabrication=fabrication_profile or {}, assembly=assembly_profile or {}, captured_at=time.time())
        context_id = self._save('contexts', record)
        return dict(context_id=context_id, context=context, suitable_as_native_precondition=record['trusted_live'],
                    missing_package_geometry=[c['id'] for c in context['components'] if not c.get('courtyard_local')])

    def _scope_path(self, context_id):
        if not re.fullmatch('[0-9a-f]{64}', context_id):
            raise ValueError('Invalid context identity')
        return self.data_dir / 'scopes' / (context_id + '.json')

    def _scope(self, context_id, value):
        _atomic(self._scope_path(context_id), _json(value))

    def active_scopes(self):
        values = []
        for path in (self.data_dir / 'scopes').glob('*.json'):
            if path.stat().st_size > 16384:
                raise ValueError('Oversized placement scope record')
            value = json.loads(path.read_bytes())
            if value.get('active'):
                values.append(value)
        return values

    async def end_managed_scope(self, context_id, reason):
        self._load('contexts', context_id)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('Record why the managed scope is abandoned')
        self._scope(context_id, dict(active=False, context_id=context_id, reason=reason, gate_passed=False))
        return dict(active=False, gate_passed=False, context_id=context_id,
                    qualification='abandoned; ordinary edits do not inherit placement approval')

    async def plan_placement(self, context_id, design_intent=None, constraints=None, options=None):
        from eda_agent.design.placement_evidence import compare_placement_candidates, generate_placement_candidates
        record = self._load('contexts', context_id)
        context, intent, rules, options = record['context'], design_intent or {}, constraints or [], options or {}
        allowed = {'engine', 'seed', 'candidate_budget', 'iterations', 'grid_mm', 'managed_scope',
                   'selection_policy', 'recovery_mode', 'candidates'}
        if set(options) - allowed:
            raise ValueError('Unknown placement search options: ' + ', '.join(sorted(set(options) - allowed)))
        recovery = options.get('recovery_mode', 'require_clean_document')
        if recovery not in {'require_clean_document', 'disk_and_pose_evidence'}:
            raise ValueError('Unsupported recovery mode')
        if 'candidates' in options:
            candidates = copy.deepcopy(options['candidates'])
        else:
            candidates = await asyncio.to_thread(generate_placement_candidates, context, intent, options)
        comparison = await asyncio.to_thread(compare_placement_candidates, context, intent, rules, candidates,
                                             options.get('selection_policy'))
        stored = dict(context_id=context_id, intent=intent, constraints=rules, options=options,
                      comparison=comparison, policy_version=POLICY_VERSION)
        plan_id = self._save('plans', stored)
        if record['trusted_live'] and options.get('managed_scope', True):
            self._scope(context_id, dict(active=True, context_id=context_id, plan_id=plan_id,
                board_path=context['board_path'], phase='preview', gate_passed=False))
        return dict(plan_id=plan_id, **comparison, native_edits=False,
                    recovery_mode=recovery, full_unsaved_buffer_recovery=False)

    async def _fresh_context(self, record):
        if not record['trusted_live']:
            raise ValueError('Caller-provided/offline geometry cannot authorize a native edit')
        _, raw = await self._snapshot(board_path=record['context']['board_path'], source='live')
        if capture_digest(raw['snapshot']) != record['capture_digest']:
            raise ValueError('STALE_PLACEMENT: board, rules, locks or copper changed; capture and preview again')
        return raw['snapshot']

    async def validate_plan(self, plan_id):
        from eda_agent.design.placement_evidence import evaluate_placement
        plan = self._load('plans', plan_id)
        context_record = self._load('contexts', plan['context_id'])
        selected = plan['comparison']['plan']
        snapshot = await self._fresh_context(context_record) if context_record['trusted_live'] else None
        report = await asyncio.to_thread(evaluate_placement, context_record['context'], plan['intent'],
            plan['constraints'], selected['moves'], candidate_id=selected['candidate_id'])
        blockers = list(report['blockers'])
        if not context_record['trusted_live']:
            blockers.append('trusted_live_capture')
        if plan['options'].get('recovery_mode', 'require_clean_document') == 'require_clean_document':
            if context_record['context'].get('document_modified') is not False:
                blockers.append('unsaved_recovery_coverage')
        if any(m.get('side') != next(c['side'] for c in context_record['context']['components']
                   if c['id'] == m['component_id']) for m in selected['moves']):
            blockers.append('native_side_change_unsupported')
        gate = dict(policy_version=POLICY_VERSION, plan_id=plan_id, context_id=plan['context_id'],
            report=report, required_checks=report['required_checks'], unresolved_blockers=sorted(set(blockers)),
            passed=report['feasible'] and not blockers, evidence_ids=[report['evidence_id']],
            source_capture_digest=context_record.get('capture_digest'),
            native_snapshot_token=(snapshot or {}).get('native_snapshot_token'),
            dependencies=['geometry', 'rules', 'intent', 'constraints', 'locks', 'policy', 'package_geometry'],
            measured_at=time.time())
        validation_id = self._save('validations', gate)
        return dict(validation_id=validation_id, **gate)

    async def apply_plan(self, plan_id, validation_id, operation_id):
        if not isinstance(operation_id, str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}', operation_id):
            raise ValueError('operation_id must be 1..64 ASCII letters, digits, underscore or hyphen')
        operation = self.data_dir / 'operations' / (operation_id + '.json')
        binding = dict(plan_id=plan_id, validation_id=validation_id)
        if operation.exists():
            previous = json.loads(operation.read_bytes())
            if previous.get('binding') != binding:
                raise ValueError('OPERATION_CONFLICT: identifier already belongs to another plan')
            if previous.get('receipt_id'):
                return dict(self._load('receipts', previous['receipt_id']), receipt_reused=True,
                            current_freshness_verified=False)
            raise ValueError('OPERATION_OUTCOME_UNKNOWN: inspect native receipt and board; no automatic retry')
        plan = self._load('plans', plan_id)
        gate = self._load('validations', validation_id)
        if gate['plan_id'] != plan_id or gate['policy_version'] != POLICY_VERSION or not gate['passed']:
            raise ValueError('A passing server-generated validation for this exact plan is required')
        record = self._load('contexts', plan['context_id'])
        selected = plan['comparison']['plan']
        async with self.native_sequence():
            snapshot = await self._fresh_context(record)
            if not snapshot.get('native_snapshot_token'):
                raise ValueError('Native placement freshness proof is unavailable')
            if not selected['moves']:
                return dict(applied=False, reason='baseline retained; no placement mutation', gate_passed=True)
            if self.checkpoint is None:
                raise ValueError('Placement requires the configured disk checkpoint service')
            checkpoint = await asyncio.to_thread(self.checkpoint, [record['context']['board_path']])
            # Original unsaved poses are evidence for a separately reviewed
            # compensation plan, not a claim of full memory/transaction undo.
            recovery = dict(checkpoint_id=checkpoint['checkpoint_id'], snapshot_kind='disk_only',
                unsaved_memory_captured=False, original_poses=record['context']['components'],
                automatic_rollback=False)
            _atomic(operation, _json(dict(binding=binding, status='dispatching', recovery=recovery)))
            try:
                native_arguments = dict(
                    board_path=record['context']['board_path'], board_identity=snapshot['board_identity'],
                    expected_native_snapshot_token=snapshot['native_snapshot_token'],
                    moves=selected['moves'], operation_id=operation_id)
                ticket = self._native_ticket.set((asyncio.current_task(), digest(native_arguments)))
                try:
                    native = await self._native('_suite_layout_apply_plan', native_arguments)
                finally:
                    self._native_ticket.reset(ticket)
                receipt = dict(plan_id=plan_id, validation_id=validation_id, operation_id=operation_id,
                    native=native, recovery=recovery, applied=native.get('success') is True,
                    qualified_placement=False, automatic_retry=False)
                if receipt['applied']:
                    _, post = await self._snapshot(board_path=record['context']['board_path'], source='live')
                    from eda_agent.design.placement_evidence import evaluate_placement
                    post_context = context_from_snapshot(post['snapshot'], 'post-apply', record.get('models', {}),
                        record.get('fabrication', {}), record.get('assembly', {}))
                    verified = evaluate_placement(post_context, plan['intent'], plan['constraints'], [])
                    receipt['post_validation'] = verified
                    receipt['qualified_placement'] = verified['feasible']
                receipt_id = self._save('receipts', receipt)
                _atomic(operation, _json(dict(binding=binding, status='completed', receipt_id=receipt_id)))
                self._scope(plan['context_id'], dict(active=True, context_id=plan['context_id'], plan_id=plan_id,
                    board_path=record['context']['board_path'], phase='applied' if receipt['applied'] else 'inspect',
                    gate_passed=receipt['qualified_placement'], receipt_id=receipt_id))
                return dict(receipt_id=receipt_id, **receipt)
            except BaseException:
                # Keep dispatching evidence. Cancellation/errors cannot release
                # this operation identity for blind repetition.
                raise

    def native_apply_authorized(self, arguments):
        ticket = self._native_ticket.get()
        return ticket is not None and ticket == (asyncio.current_task(), digest(arguments))

    def guard_native_effect(self, backend, name, arguments):
        """Managed scopes reject unbound geometry/opaque writes, including aliases.

        The local suite cannot gate another external Altium client. This guard
        does not assert that a user's arbitrary script is a verified workflow.
        """
        if not self.active_scopes():
            return
        from suite_layout_policy import native_effects
        effects = native_effects(backend, name, arguments)
        if effects & {'placement', 'opaque', 'geometry', 'rules'}:
            raise ValueError('MANAGED_PLACEMENT_SCOPE: use the reviewed layout plan, or explicitly '
                             'end_managed_scope to abandon qualification; stage_result cannot bypass this guard')
