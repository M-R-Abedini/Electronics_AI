"""Evidence-bound placement over the existing copper reader and PCB solvers.

SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import asyncio
import copy
import contextvars
import hashlib
import json
import os
from pathlib import Path
import re
import time

from suite_si_pi import SiPiService, _json, _path
from suite_checkpoints import _atomic, digest

P0_POLICY_VERSION = 'placement-evidence-p0-v4'
P1_POLICY_VERSION = 'placement-evidence-p1-v1'
POLICY_VERSION = P1_POLICY_VERSION
SUPPORTED_POLICIES = (P0_POLICY_VERSION, P1_POLICY_VERSION)
GATE_REVISION = 'placement-copper-p2-v1'

# Bridge/session diagnostics have no physical meaning. Keep this allowlist
# explicit: rules, lock state, dirty state and unknown fields still bind plans.
CAPTURE_TRANSPORT_FIELDS = frozenset({
    'native_snapshot_token', 'native_snapshot_json', 'capture_metrics',
    'provenance', '_hint_detach',
})


def bounded_checks(value):
    """Keep full immutable evidence on disk and bound routine MCP responses.

    Prioritize failures/unknowns over passing geometry rows. Explicit
    get_evidence calls retain the complete artifact for deliberate inspection.
    """
    if isinstance(value, list):
        return [bounded_checks(item) for item in value]
    if not isinstance(value, dict):
        return value
    identifiers = {'blockers', 'required_checks', 'unresolved_blockers'}
    result = {key: bounded_checks(item) for key, item in value.items() if key not in identifiers | {'checks'}}
    for key in identifiers & value.keys():
        items = value[key]
        if isinstance(items, list):
            result[key] = [bounded_checks(item) for item in items[:64]]
            result[key + '_total'] = len(items)
            result[key + '_truncated'] = len(items) > 64
        else:
            result[key] = bounded_checks(items)
    if isinstance(value.get('checks'), list):
        checks = value['checks']
        important = [c for c in checks if c.get('status') != 'pass']
        selected = (important + [c for c in checks if c.get('status') == 'pass'])[:64]
        result.update(checks=selected, checks_total=len(checks), checks_truncated=len(checks) > len(selected))
    elif 'checks' in value:
        result['checks'] = value['checks']
    return result


def capture_digest(snapshot):
    """Bind live rules, locks and geometry; exclude only capture transport data."""
    return digest(canonical_snapshot_records({k: v for k, v in snapshot.items() if k not in CAPTURE_TRANSPORT_FIELDS}))


def canonical_snapshot_records(snapshot):
    """PCB iterator order changes after BeginModify; object identity does not.

    Sort only unordered primitive collections, never contour vertices, layer
    stacks or rule priorities. Every field and each duplicate remain bound.
    """
    result = dict(snapshot)
    for key in ('components','pads','holes','tracks','arcs','vias','regions','keepouts','cutouts','net_ties'):
        if isinstance(result.get(key),list):
            result[key] = sorted(result[key], key=lambda row: (str(row.get('id','')),
                str(row.get('owner_id','')), digest(row)) if isinstance(row,dict) else ('','',digest(row)))
    return result


def unexpected_post_changes(before, after, context, moves):
    """Compare the whole captured state, permitting only planned rigid poses.

    Derived AABBs of moved packages are requalified by the geometry evaluator;
    they cannot be predicted from an old AABB after an arbitrary rotation.
    Untouched copper, rule participation, locks and object identities remain
    dependencies. Unsupported component-owned copper changes fail closed.
    """
    from eda_agent.design.placement_evidence import transformed_pads, transform_point, inverse_transform_point
    expected, observed = copy.deepcopy(before), copy.deepcopy(after)
    old = {c['id']: c for c in context['components']}
    poses = {c['id']: {k: c[k] for k in ('x_mm', 'y_mm', 'rotation_deg', 'side')} for c in context['components']}
    changed = {m['component_id']: m for m in moves}
    for m in moves:
        poses[m['component_id']].update({k: m[k] for k in ('x_mm', 'y_mm', 'rotation_deg', 'side')})
    projected = transformed_pads(context, poses)
    errors = []
    actual_components = {c['id']: c for c in observed.get('components', [])}
    def normalise_position(actual, target, label):
        for key in ('x_mm', 'y_mm', 'rotation_deg'):
            if key not in target:
                continue
            value = actual.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                errors.append(label); continue
            difference = (abs((value-target[key]+180) % 360-180) if key == 'rotation_deg'
                          else abs(value-target[key]))
            if difference > (.000001 if key == 'rotation_deg' else .0000031):
                errors.append(label)
            actual[key] = target[key]
    for comp in expected.get('components', []):
        cid = comp['id']
        if cid not in changed:
            continue
        comp.update({k: changed[cid][k] for k in ('x_mm', 'y_mm', 'rotation_deg', 'side')})
        actual = actual_components.get(cid)
        if actual is None:
            errors.append('component:'+cid); continue
        normalise_position(actual, comp, 'component:'+cid)
        comp.pop('bbox_mm', None); actual.pop('bbox_mm', None)
    actual_pads = {str(p['id']): p for p in observed.get('pads', [])}
    moved_pad_owners = {}
    for pad in expected.get('pads', []):
        pid = str(pad['id'])
        if pid not in projected or projected[pid].get('component_id') not in changed:
            continue
        moved_pad_owners[pid] = projected[pid]['component_id']
        pad.update({k: projected[pid][k] for k in ('x_mm', 'y_mm', 'rotation_deg')})
        if pid not in actual_pads:
            errors.append('pad:'+pid); continue
        normalise_position(actual_pads[pid], pad, 'pad:'+pid)
        if context.get('policy_version') == P1_POLICY_VERSION:
            for field in ('layer_geometries','hole_geometry'):
                if field not in projected[pid]:
                    continue
                pad[field] = copy.deepcopy(projected[pid][field])
                target_rows = pad[field] if isinstance(pad[field],list) else [pad[field]]
                actual_value = actual_pads[pid].get(field)
                actual_rows = actual_value if isinstance(actual_value,list) else [actual_value]
                if len(actual_rows)!=len(target_rows) or any(not isinstance(r,dict) for r in actual_rows):
                    errors.append('pad:'+pid+':'+field); continue
                for a,b in zip(actual_rows,target_rows):
                    normalise_position(a,b,'pad:'+pid+':'+field)
                    for key in ('bbox_mm','pad_rotation_deg'):
                        if key not in b: continue
                        av,bv=a.get(key),b[key]
                        aa=av if isinstance(av,list) else [av]
                        bb=bv if isinstance(bv,list) else [bv]
                        if len(aa)!=len(bb) or any(not isinstance(v,(int,float)) or abs(v-w)>.0000031 for v,w in zip(aa,bb)):
                            errors.append('pad:'+pid+':'+field+':'+key)
                        a[key]=copy.deepcopy(bv)
    hole_key = lambda p: str(p.get('id', 'pad:'+str(p.get('owner_id'))))
    actual_holes = {hole_key(p): p for p in observed.get('holes', [])}
    for hole in expected.get('holes', []):
        owner = moved_pad_owners.get(str(hole.get('owner_id')))
        if owner is None:
            continue
        point = transform_point(inverse_transform_point((hole['x_mm'], hole['y_mm']), old[owner]), poses[owner])
        hole.update(x_mm=point[0], y_mm=point[1])
        if hole_key(hole) in actual_holes:
            normalise_position(actual_holes[hole_key(hole)], hole, 'hole:'+hole_key(hole))
        elif str(hole.get('owner_id')) in actual_pads:
            # Native pad-owned drill entries may have no separate primitive ID.
            actual_hole = next((h for h in observed.get('holes',[])
                                if h.get('owner_id')==hole.get('owner_id')),None)
            if actual_hole is not None:
                normalise_position(actual_hole,hole,'hole:'+str(hole.get('owner_id')))
        if context.get('policy_version') == P1_POLICY_VERSION and hole.get('hole_geometry'):
            raw_pad=projected.get(str(hole.get('owner_id')), {})
            if raw_pad.get('hole_geometry'):
                hole['hole_geometry']=copy.deepcopy(raw_pad['hole_geometry'])
                actual_hole=next((h for h in observed.get('holes',[]) if h.get('owner_id')==hole.get('owner_id')),None)
                if actual_hole and actual_hole.get('hole_geometry'):
                    normalise_position(actual_hole['hole_geometry'],hole['hole_geometry'],'hole_geometry')
    ignored = CAPTURE_TRANSPORT_FIELDS | {'document_modified'}
    expected, observed = canonical_snapshot_records(expected), canonical_snapshot_records(observed)
    for key in set(expected) | set(observed):
        if key not in ignored and digest(expected.get(key)) != digest(observed.get(key)):
            errors.append('dependency:'+key)
    return sorted(set(errors))


def context_from_snapshot(snapshot, snapshot_id, models, fabrication, assembly, policy_version=POLICY_VERSION):
    """Use explicit pad ownership. A native AABB never becomes a courtyard."""
    components, references = [], {}
    allowed_model = {'courtyard_local', 'courtyard_source', 'body_local', 'height_mm',
                     'through_body', 'allowed_rotations_deg', 'allowed_sides', 'overhang'}
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
                                   'top' if 'top' in layer.lower() else None)
        if side not in {'top', 'bottom'}:
            side = None
        components.append(dict(model, id=identity, reference=ref, x_mm=row['x_mm'], y_mm=row['y_mm'],
            rotation_deg=row.get('rotation_deg', 0), side=side, locked=row.get('locked') is True,
            fixed=row.get('locked') is not False))
    unknown_models = set(models) - set(references) - set(references.values())
    if unknown_models:
        raise ValueError('Geometry models reference uncaptured components: ' + ', '.join(sorted(unknown_models)))
    pads, stationary_pads = [], []
    unknown_ownership = False
    for row in snapshot['pads']:
        owner = references.get(row.get('component'))
        if owner is None:
            # Absolute, fixed board copper remains in clearance/channel checks.
            # A nonblank unresolvable owner also makes pad extraction unknown:
            # that pad might move with a component whose geometry is omitted.
            unknown_ownership |= bool(row.get('component'))
            stationary_pads.append(copy.deepcopy(row))
        else:
            pads.append(dict(copy.deepcopy(row), component_id=owner))
    source_coverage = snapshot.get('coverage', {})
    def complete(section):
        value = source_coverage.get(section)
        if value is None:
            return snapshot.get('complete') is True
        if isinstance(value, dict):
            return (value.get('complete') is not False
                    and value.get('status') not in {'partial', 'unknown', 'unsupported', 'unavailable'}
                    and (value.get('complete') is True or value.get('status') == 'complete'))
        return value is True or value == 'complete'
    # Native shares one section for physical board cutouts and per-layer
    # polygon pour voids. Only the former removes FR4 on every board layer.
    # Copper voids can overlap each other across layers and must not become
    # holes of the mechanical outline. Preserve all records in copper_snapshot.
    from eda_agent.design.copper_geometry import region_geometry_valid
    def mechanical_region_valid(outline, holes=()):
        try:
            return region_geometry_valid({'outline': outline, 'holes': list(holes)})
        except (TypeError, ValueError, KeyError, IndexError):
            return False
    cutouts_complete = complete('cutouts') if 'cutouts' in source_coverage else complete('regions')
    cutouts = []
    for row in snapshot.get('cutouts', []):
        if isinstance(row, dict) and row.get('kind') == 'polygon_cutout':
            continue
        if (not isinstance(row, dict) or row.get('kind') != 'board_cutout'
                or row.get('geometry_complete') is False or row.get('holes')
                or not mechanical_region_valid(row.get('outline', []))):
            # An unclassified or unsupported physical contour is not a guessed
            # hole. Keep the data, but disallow a qualified outline result.
            cutouts_complete = False
            continue
        cutouts.append(copy.deepcopy(row['outline']))
    outline = copy.deepcopy(snapshot.get('board_outline') or None)
    geometry_diagnostics = []
    from eda_agent.design.copper_geometry import polygon_area
    large_cutout = (policy_version == P1_POLICY_VERSION and outline
        and any(abs(polygon_area(c)) > .9*abs(polygon_area(outline)) for c in cutouts))
    if outline is not None and (not mechanical_region_valid(outline, cutouts) or large_cutout):
        from suite_board_geometry import subtract_rectilinear_cutouts
        try:
            composed = subtract_rectilinear_cutouts(outline, cutouts,
                ignore_full_board_artifacts=policy_version == P1_POLICY_VERSION)
        except ValueError as error:
            geometry_diagnostics.append(dict(section='physical_board_shape', status='unknown',
                reason=str(error) + '; raw evidence retained'))
            cutouts_complete = False
            cutouts = []
            if not mechanical_region_valid(outline):
                outline = None
        else:
            outline, cutouts = composed['outline'], composed['holes']
            geometry_diagnostics.append(dict(section='physical_board_shape', status='resolved',
                method='exact_rational_rectilinear_subtraction', raw_evidence_retained=True))
            if composed.get('warnings'):
                geometry_diagnostics.extend(composed['warnings'])
                geometry_diagnostics.append({'ignored_full_board_cutouts':composed['ignored_full_board_cutouts'],
                    'qualification':'placement_policy_override_only; physical_board_manufacturing_not_certified'})
    outline_complete = outline is not None and complete('board_outline') and cutouts_complete
    pads_complete = complete('pads') and not unknown_ownership
    result = dict(schema=1, units='mm', board_path=snapshot['board_path'],
        board_identity=snapshot.get('board_identity'), snapshot_id=snapshot_id,
        source=snapshot['source'], document_modified=snapshot.get('document_modified'),
        unsaved_detection_complete=snapshot.get('unsaved_detection_complete') is True,
        outline=outline, cutouts=cutouts, geometry_diagnostics=geometry_diagnostics,
        layers=copy.deepcopy(snapshot['layers']), components=components, pads=pads, stationary_pads=stationary_pads,
        pad_ownership_complete=not unknown_ownership,
        fabrication_profile=copy.deepcopy(fabrication), assembly_profile=copy.deepcopy(assembly),
        coverage={'outline': 'complete' if outline_complete else 'unknown',
                  'physical_cutouts': 'complete' if cutouts_complete else 'unknown',
                  'pads': 'complete' if pads_complete else 'unknown',
                  'routing_obstacles': 'complete' if all(complete(s) for s in
                      ('tracks','arcs','vias','regions','layers')) else 'partial'},
        native_rules=copy.deepcopy(snapshot.get('native_rules')),
        copper_snapshot=copy.deepcopy(snapshot),
        renderer_coverage={'courtyard': 'unavailable', 'pours': 'unavailable',
                           'electrical_verification': False})
    if snapshot.get('origin_mm') is not None:
        result['origin_mm'] = copy.deepcopy(snapshot['origin_mm'])
    if policy_version not in SUPPORTED_POLICIES:
        raise ValueError('Unsupported placement policy')
    result['policy_version'] = policy_version
    if policy_version == P1_POLICY_VERSION:
        from eda_agent.design.placement_p1 import prepare_context
        result = prepare_context(result)
    return result


def list_tools():
    obj = {'type': 'object'}
    string = {'type': 'string'}
    def tool(name, description, properties, required=()):
        return dict(name=name, description=description, inputSchema=dict(type='object',
            properties=properties, required=list(required), additionalProperties=False))
    return [
        tool('get_contracts', 'Discover the actual placement context, intent and constraint contracts; no CAD call.', {}),
        tool('get_workflow', 'Read the existing design state machine for this placement scope; caller claims never pass its verified gates.',
             dict(context_id=string), ['context_id']),
        tool('capture_context', 'Capture an explicit loaded PCB, or register caller-provided offline geometry. Does not save or edit Altium.',
             dict(board_path=string, board_context=obj, package_geometry=obj, fabrication_profile=obj, assembly_profile=obj,
                  policy_version={'enum':list(SUPPORTED_POLICIES)},
                  scope_component_ids={'type': 'array', 'items': string, 'uniqueItems': True}, input_gate=obj)),
        tool('plan_placement', 'Reuse seeded PCB solvers, compare baseline and alternatives with hard constraints outside score. No CAD edits.',
             dict(context_id=string, design_intent=obj, constraints={'type': 'array', 'items': obj}, options=obj), ['context_id']),
        tool('validate_plan', 'Re-evaluate the exact stored moves against fresh live geometry and recovery policy; caller stage results cannot pass this gate.',
             dict(plan_id=string), ['plan_id']),
        tool('apply_plan', 'Apply the exact validated plan once, with checkpoint, native freshness and readback; never rerun the optimizer.',
             dict(plan_id=string, validation_id=string, operation_id=string), ['plan_id', 'validation_id', 'operation_id']),
        tool('revert_in_memory', 'Compensate the latest exact applied placement with its captured original poses, without saving or closing Altium. Refuses intervening physical edits; not general undo.',
             dict(plan_id=string)),
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
        self.data_dir = Path(data_dir or Path.home() / '.electronics-ai').resolve() / 'placement'
        self.checkpoint = checkpoint
        self._lock = asyncio.Lock()
        self._native_ticket = contextvars.ContextVar('placement_native_ticket', default=None)

    list_tools = staticmethod(list_tools)

    async def call_tool_async(self, name, arguments):
        if name == 'get_contracts':
            from eda_agent.design.placement_evidence import contract_schemas
            schemas=contract_schemas()
            schemas['policy_version']=POLICY_VERSION
            schemas['BoardContext']['properties']['policy_version']={'enum':list(SUPPORTED_POLICIES)}
            schemas['UnifiedBoardModel']={'type':'object','required':['context'],
                'properties':{'context':schemas['BoardContext'],'capture_digest':{'type':['string','null']},
                              'snapshot':{'type':['object','null']}},'additionalProperties':True,
                'description':'Lossless immutable adapter of one capture record; unknown fields retained'}
            return dict(policy_version=POLICY_VERSION, gate_revision=GATE_REVISION, schemas=schemas,
                        supported_policy_versions=list(SUPPORTED_POLICIES),
                        search_modes=['refine','construct','hierarchical','cluster_scoped'],
                        p1_assumptions={'derived_envelope_margins':'user-authorized engineering defaults; not IPC certification',
                            'large_cutout':'explicit reported area heuristic; no native contour removal',
                            'board_rule_clearance':'design constraint; fabricator capability remains unverified'},
                        native_apply={'side_changes': False, 'transactional_rollback': False,
                                      'revert_in_memory':True,
                                      'recovery': 'disk_checkpoint_or_freshness_bound_original_pose_compensation'})
        if name == 'get_evidence':
            return await asyncio.to_thread(self._load, arguments['kind'], arguments['evidence_id'])
        method = getattr(self, name, None)
        if name not in {t['name'] for t in list_tools()} or method is None:
            raise ValueError('Unknown placement capability')
        async with self._lock:
            return bounded_checks(await method(**arguments))

    async def capture_context(self, board_path=None, board_context=None, package_geometry=None,
                              fabrication_profile=None, assembly_profile=None, scope_component_ids=None, input_gate=None,
                              policy_version=None):
        from eda_agent.design.placement_evidence import validate_board_context
        if input_gate is not None:
            from eda_agent.design.project_gate import evaluate_project_gate
            readiness = evaluate_project_gate(input_gate['project_dir'], input_gate['safety_requirements'])
            if not readiness['passed'] or not board_path:
                return dict(context_id=None, input_gate=readiness, native_edits=False,
                    suitable_as_native_precondition=False, status=readiness['status'] if not readiness['passed']
                    else 'ready_requires_explicit_board_path')
            if _path(board_path) != _path(readiness['target_board_path']):
                raise ValueError('ERR_TARGET_DOCUMENT_MISMATCH: input gate and capture target differ')
        if board_context is not None:
            context = copy.deepcopy(board_context)
            context['policy_version'] = policy_version or context.get('policy_version', POLICY_VERSION)
            if context['policy_version'] not in SUPPORTED_POLICIES:
                raise ValueError('Unsupported placement policy')
            if context['policy_version'] == P1_POLICY_VERSION:
                from eda_agent.design.placement_p1 import prepare_context
                context = prepare_context(context)
            if board_path and _path(board_path) != _path(context['board_path']):
                raise ValueError('Offline context targets another board')
            context['source'] = 'caller_provided'
            if scope_component_ids is not None:
                context['scope_component_ids'] = scope_component_ids
            context = validate_board_context(context)
            record = dict(context=context, trusted_live=False, snapshot=None, captured_at=time.time())
        else:
            snapshot_id, raw = await self._snapshot(board_path=board_path, source='live')
            context = context_from_snapshot(raw['snapshot'], snapshot_id, package_geometry or {},
                                            fabrication_profile or {}, assembly_profile or {}, policy_version or POLICY_VERSION)
            if scope_component_ids is not None:
                context['scope_component_ids'] = scope_component_ids
            context = validate_board_context(context)
            record = dict(context=context, trusted_live=raw['trusted_live'], snapshot=raw['snapshot'],
                capture_digest=capture_digest(raw['snapshot']), models=package_geometry or {},
                fabrication=fabrication_profile or {}, assembly=assembly_profile or {}, captured_at=time.time())
        if input_gate is not None:
            record['input_gate_request'] = copy.deepcopy(input_gate)
            record['input_gate_digest'] = readiness['evidence_id']
        context_id = self._save('contexts', record)
        return dict(context_id=context_id, context=context, suitable_as_native_precondition=record['trusted_live'],
                    missing_package_geometry=[c['id'] for c in context['components'] if not c.get('courtyard_local')])

    def _scope_path(self, context_id):
        if not re.fullmatch('[0-9a-f]{64}', context_id):
            raise ValueError('Invalid context identity')
        return self.data_dir / 'scopes' / (context_id + '.json')

    def _scope(self, context_id, value):
        _atomic(self._scope_path(context_id), _json(value))

    def _journal(self, context_id):
        from eda_agent.design.session import SessionStore
        store = SessionStore(self.data_dir / 'sessions')
        journal = store.get(context_id)
        if not journal.path.exists():
            journal = store.start('Evidence-bound placement review of an existing board',
                                  context_id, workflow='placement_review')
        return journal

    async def get_workflow(self, context_id):
        from dataclasses import asdict
        from eda_agent.design.state_machine import next_action
        record = self._load('contexts', context_id)
        freshness = 'offline_not_qualified'
        if record['trusted_live']:
            expected = record['capture_digest']
            scope = next((s for s in self.active_scopes() if s['context_id']==context_id), {})
            if scope.get('receipt_id'):
                expected = self._load('receipts', scope['receipt_id']).get('post_capture_digest')
            try:
                _, current = await self._snapshot(board_path=record['context']['board_path'], source='live')
                freshness = 'fresh' if expected and capture_digest(current['snapshot'])==expected else 'stale'
            except Exception:
                freshness = 'unknown'
            if freshness != 'fresh':
                for stage in ('placement', 'verification'):
                    self._journal(context_id).verified_gate(stage, context_id, context_id,
                        record['context'].get('policy_version', POLICY_VERSION),
                        passed=False, checks=['fresh_live_evidence_required'])
        state = self._journal(context_id).state()
        return dict(state=asdict(state), next_action=asdict(next_action(state)), freshness=freshness,
                    qualification_scope='placement_only; routing, DRC and fabrication release are separate')

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
        allowed = {'mode', 'seed', 'candidate_count', 'iterations', 'search_grid_mm', 'managed_scope',
                   'restart_radius_mm', 'proposal_clearance_mm', 'selection_policy', 'recovery_mode', 'candidates'}
        if set(options) - allowed:
            raise ValueError('Unknown placement search options: ' + ', '.join(sorted(set(options) - allowed)))
        recovery = options.get('recovery_mode', 'require_clean_document')
        if recovery not in {'require_clean_document', 'disk_and_pose_evidence'}:
            raise ValueError('Unsupported recovery mode')
        hierarchy = None
        if 'candidates' in options:
            candidates = copy.deepcopy(options['candidates'])
        elif options.get('mode') == 'cluster_scoped':
            from eda_agent.design.cluster_placement import UnifiedBoardModel,ScopedCopperClusterRefiner
            hierarchy=await asyncio.to_thread(ScopedCopperClusterRefiner().propose,
                UnifiedBoardModel.from_capture(record),intent,options)
            candidates=hierarchy['candidates']
        elif options.get('mode') == 'hierarchical':
            if context.get('policy_version') != P1_POLICY_VERSION:
                raise ValueError('Hierarchical proposals require an explicitly captured P1 context')
            from eda_agent.design.cluster_placement import UnifiedBoardModel, HierarchicalClusterPlacer
            hierarchy = await asyncio.to_thread(HierarchicalClusterPlacer().propose,
                UnifiedBoardModel.from_capture(record), intent, options)
            candidates = hierarchy['candidates']
        else:
            candidates = await asyncio.to_thread(generate_placement_candidates, context, intent, options)
        comparison = await asyncio.to_thread(compare_placement_candidates, context, intent, rules, candidates,
                                             options.get('selection_policy'))
        if hierarchy is not None:
            comparison['hierarchical_analysis'] = {k:v for k,v in hierarchy.items() if k!='candidates'}
        comparison['plan'].update(target_board_path=context['board_path'],
            board_revision_digest=record.get('capture_digest') or comparison['input_digest'],
            exact_moves=copy.deepcopy(comparison['plan']['moves']),
            diff_vs_baseline=[{'component_id':m['component_id'],
                'before':next(c for c in context['components'] if c['id']==m['component_id']),
                'after':m} for m in comparison['plan']['moves']])
        comparison['plan']['plan_digest'] = digest({k:v for k,v in comparison['plan'].items() if k!='plan_digest'})
        stored = dict(context_id=context_id, intent=intent, constraints=rules, options=options,
                      comparison=comparison, policy_version=context.get('policy_version', POLICY_VERSION))
        plan_id = self._save('plans', stored)
        journal = self._journal(context_id)
        journal.plan_revision(journal.state().plan_revision + 1, plan_hash=plan_id)
        journal.artifact(plan_id, kind='placement_plan')
        if record['trusted_live'] and options.get('managed_scope', True):
            self._scope(context_id, dict(active=True, context_id=context_id, plan_id=plan_id,
                board_path=context['board_path'], phase='preview', gate_passed=False))
        return dict(plan_id=plan_id, **comparison, native_edits=False,
                    recovery_mode=recovery, full_unsaved_buffer_recovery=False)

    async def _fresh_context(self, record):
        if not record['trusted_live']:
            raise ValueError('Caller-provided/offline geometry cannot authorize a native edit')
        if record.get('input_gate_request'):
            from eda_agent.design.project_gate import evaluate_project_gate
            current = evaluate_project_gate(**record['input_gate_request'])
            if not current['passed'] or current['evidence_id'] != record['input_gate_digest']:
                raise ValueError('STALE_PLACEMENT_INPUT: project artifact or declared input gate changed')
        _, raw = await self._snapshot(board_path=record['context']['board_path'], source='live')
        if capture_digest(raw['snapshot']) != record['capture_digest']:
            raise ValueError('STALE_PLACEMENT: board, rules, locks or copper changed; capture and preview again')
        return raw['snapshot']

    async def validate_plan(self, plan_id):
        from eda_agent.design.placement_evidence import evaluate_placement,compare_placement_candidates
        plan = self._load('plans', plan_id)
        if plan.get('policy_version') not in SUPPORTED_POLICIES:
            raise ValueError('STALE_PLACEMENT_POLICY: generate a new plan under the current server policy')
        context_record = self._load('contexts', plan['context_id'])
        if context_record['context'].get('policy_version', POLICY_VERSION) != plan['policy_version']:
            raise ValueError('STALE_PLACEMENT_POLICY: plan and captured context policies differ')
        selected = plan['comparison']['plan']
        snapshot = await self._fresh_context(context_record) if context_record['trusted_live'] else None
        report = await asyncio.to_thread(evaluate_placement, context_record['context'], plan['intent'],
            plan['constraints'], selected['moves'], candidate_id=selected['candidate_id'])
        blockers = list(report['blockers'])
        if selected['moves']:
            comparison=await asyncio.to_thread(compare_placement_candidates,context_record['context'],
                plan['intent'],plan['constraints'],[dict(id='gate-exact-moves',moves=selected['moves'])],
                plan['options'].get('selection_policy'))
            if comparison['selection_status']!='selected':blockers.append('current_tier_1_selection')
        if not selected.get('apply_eligible'):
            blockers.append('candidate_selection_blocked')
        if not context_record['trusted_live']:
            blockers.append('trusted_live_capture')
        if selected['moves'] and plan['options'].get('recovery_mode', 'require_clean_document') == 'require_clean_document':
            if (context_record['context'].get('document_modified') is not False or
                    not context_record['context'].get('unsaved_detection_complete')):
                blockers.append('unsaved_recovery_coverage')
        if selected['moves'] and context_record['trusted_live']:
            from suite_placement_adapter import prepare_layout_native_batch
            try:
                prepare_layout_native_batch(context_record['context']['board_path'],
                    (snapshot or {}).get('board_identity'), (snapshot or {}).get('native_snapshot_token'),
                    selected['moves'], 'gate_transport_validation')
            except ValueError:
                blockers.append('native_transport_unsupported')
        if any(m.get('side') != next(c['side'] for c in context_record['context']['components']
                   if c['id'] == m['component_id']) for m in selected['moves']):
            blockers.append('native_side_change_unsupported')
        gate = dict(policy_version=plan['policy_version'], gate_revision=GATE_REVISION, plan_id=plan_id, context_id=plan['context_id'],
            report=report, required_checks=report['required_checks'], unresolved_blockers=sorted(set(blockers)),
            passed=report['feasible'] and not blockers, evidence_ids=[report['evidence_id']],
            source_capture_digest=context_record.get('capture_digest'),
            native_snapshot_token=(snapshot or {}).get('native_snapshot_token'),
            dependencies=['geometry', 'rules', 'intent', 'constraints', 'locks', 'policy', 'package_geometry'],
            measured_at=time.time())
        validation_id = self._save('validations', gate)
        self._journal(plan['context_id']).verified_gate('placement', validation_id,
            context_record['context'].get('snapshot_id') or plan['context_id'], plan['policy_version'],
            passed=gate['passed'], checks=gate['required_checks'])
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
        if plan.get('policy_version') not in SUPPORTED_POLICIES:
            raise ValueError('STALE_PLACEMENT_POLICY: old policy cannot authorize a native edit')
        gate = self._load('validations', validation_id)
        if (gate.get('gate_revision') != GATE_REVISION or gate['plan_id'] != plan_id
                or gate['policy_version'] != plan['policy_version'] or not gate['passed']):
            raise ValueError('A passing server-generated validation for this exact plan is required')
        record = self._load('contexts', plan['context_id'])
        if record['context'].get('policy_version', POLICY_VERSION) != plan['policy_version']:
            raise ValueError('STALE_PLACEMENT_POLICY: plan and captured context policies differ')
        selected = plan['comparison']['plan']
        async with self.native_sequence():
            snapshot = await self._fresh_context(record)
            if not snapshot.get('native_snapshot_token'):
                raise ValueError('Native placement freshness proof is unavailable')
            if not selected['moves']:
                self._journal(plan['context_id']).verified_gate('verification', validation_id,
                    record['context'].get('snapshot_id') or plan['context_id'], plan['policy_version'],
                    passed=True, checks=gate['required_checks'])
                return dict(applied=False, reason='baseline retained; no placement mutation', gate_passed=True)
            if self.checkpoint is None:
                raise ValueError('Placement requires the configured disk checkpoint service')
            checkpoint = await asyncio.to_thread(self.checkpoint, [record['context']['board_path']])
            # Original unsaved poses are evidence for a separately reviewed
            # compensation plan, not a claim of full memory/transaction undo.
            recovery = dict(checkpoint_id=checkpoint['checkpoint_id'], snapshot_kind='disk_only',
                unsaved_memory_captured=False, original_poses=record['context']['components'],
                automatic_rollback=False)
            # Exclusive first publication across suite processes. A concurrent
            # caller cannot overwrite an earlier operation or its receipt.
            operation.parent.mkdir(parents=True, exist_ok=True)
            claim = operation.with_name(operation.name + '.' + os.urandom(12).hex() + '.tmp')
            try:
                _atomic(claim, _json(dict(binding=binding, status='dispatching', recovery=recovery)))
                try:
                    os.link(claim, operation)
                except FileExistsError as exc:
                    raise ValueError('Concurrent operation already claimed; inspect its receipt') from exc
            finally:
                claim.unlink(missing_ok=True)
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
                    qualified_placement=False, automatic_retry=False,
                    policy_version=plan['policy_version'],
                    qualification_scope='placement under recorded policy assumptions; not manufacturing/3D/routing certification',
                    possible_partial_native_edit=native.get('possible_partial_native_edit') is True)
                receipt.update(affected_routed_nets=selected.get('affected_routed_nets',[]),
                    routing_repair_required=selected.get('routing_repair_required',False))
                receipt.update(target_board_path=record['context']['board_path'],
                    board_revision_digest=record['capture_digest'], exact_moves=selected['moves'],
                    applied_objects=native.get('applied_ids', []), skipped_locked_objects=native.get('skipped_locked_objects', []),
                    failed_objects=native.get('failed_ids', []), readback_coords=native.get('readback', []))
                if receipt['applied']:
                    _, post = await self._snapshot(board_path=record['context']['board_path'], source='live')
                    from eda_agent.design.placement_evidence import evaluate_placement, compare_placement_candidates
                    post_context = context_from_snapshot(post['snapshot'], 'post-apply', record.get('models', {}),
                        record.get('fabrication', {}), record.get('assembly', {}), plan['policy_version'])
                    post_context['scope_component_ids'] = ([m['component_id'] for m in selected['moves']]
                        if plan['policy_version']==P1_POLICY_VERSION else record['context']['scope_component_ids'])
                    verified = evaluate_placement(post_context, plan['intent'], plan['constraints'], [])
                    actual = {c['id']: c for c in post_context['components']}
                    # One native coordinate plus JSON decimal serialization
                    # error, not an engineering/design-clearance allowance.
                    pose_errors = [m['component_id'] for m in selected['moves']
                        if m['component_id'] not in actual or any(
                            abs(actual[m['component_id']][axis] - m[axis]) > 0.0000031
                            for axis in ('x_mm', 'y_mm')) or
                        abs((actual[m['component_id']]['rotation_deg'] - m['rotation_deg'] + 180) % 360 - 180) > .000001 or
                        actual[m['component_id']]['side'] != m['side']]
                    receipt['post_validation'] = verified
                    receipt['post_capture_digest'] = capture_digest(post['snapshot'])
                    receipt['post_pose_mismatch_ids'] = pose_errors
                    actual_moves = [{**m, **{k:actual[m['component_id']][k] for k in ('x_mm','y_mm','rotation_deg','side')}}
                                    for m in selected['moves'] if m['component_id'] in actual]
                    post_comparison = compare_placement_candidates(record['context'], plan['intent'], plan['constraints'],
                        [{'id':'post-apply-readback','moves':actual_moves}], plan['options'].get('selection_policy'))
                    receipt['post_tier_1_regressions'] = post_comparison['tradeoffs'][-1]['critical_regressions']
                    receipt['post_improvement_verified'] = post_comparison['selection_status']=='selected'
                    changed_dependencies = unexpected_post_changes(record['snapshot'], post['snapshot'],
                                                                    record['context'], selected['moves'])
                    receipt['unexpected_post_changes'] = changed_dependencies
                    receipt['qualified_placement'] = (verified['feasible'] and not pose_errors and not changed_dependencies
                        and not receipt['post_tier_1_regressions'] and receipt['post_improvement_verified'])
                receipt_id = self._save('receipts', receipt)
                journal = self._journal(plan['context_id'])
                journal.verified_gate('verification', receipt_id,
                    record['context'].get('snapshot_id') or plan['context_id'], plan['policy_version'],
                    passed=receipt['qualified_placement'], checks=gate['required_checks'])
                if not receipt['applied']:
                    journal.stage_result('verification', 'failed', verdict='Inspect native partial/unverified receipt; no automatic replay')
                _atomic(operation, _json(dict(binding=binding, status='completed', receipt_id=receipt_id)))
                self._scope(plan['context_id'], dict(active=True, context_id=plan['context_id'], plan_id=plan_id,
                    board_path=record['context']['board_path'], phase='applied' if receipt['applied'] else 'inspect',
                    gate_passed=receipt['qualified_placement'], receipt_id=receipt_id))
                return dict(receipt_id=receipt_id, **receipt)
            except BaseException:
                # Keep dispatching evidence. Cancellation/errors cannot release
                # this operation identity for blind repetition.
                raise

    async def revert_in_memory(self, plan_id=None):
        scopes = [s for s in self.active_scopes() if s.get('phase') in {'applied','reverted'}
                  and (plan_id is None or s.get('plan_id') == plan_id)]
        if plan_id is None and scopes:
            scopes=[max(scopes,key=lambda s:self._scope_path(s['context_id']).stat().st_mtime_ns)]
        if len(scopes) != 1:
            raise ValueError('Name exactly one applied plan; no unambiguous active placement to revert')
        scope = scopes[0]
        receipt = self._load('receipts', scope['receipt_id'])
        if scope['phase'] == 'reverted':
            return dict(receipt, receipt_id=scope['receipt_id'], receipt_reused=True,
                        current_freshness_verified=False)
        if not receipt.get('applied') or not receipt.get('post_capture_digest'):
            raise ValueError('A completed applied receipt with post-state evidence is required')
        plan_id = scope['plan_id']
        plan = self._load('plans', plan_id)
        record = self._load('contexts', plan['context_id'])
        original = {c['id']:c for c in record['context']['components']}
        moves = [dict(component_id=m['component_id'],designator=original[m['component_id']]['reference'],
                      **{k:original[m['component_id']][k] for k in ('x_mm','y_mm','rotation_deg','side')})
                 for m in receipt['exact_moves']]
        operation_id = 'revert_' + scope['receipt_id'][:40]
        operation = self.data_dir / 'operations' / (operation_id + '.json')
        async with self.native_sequence():
            if operation.exists():
                op=json.loads(operation.read_bytes())
                if op.get('receipt_id'):
                    return dict(self._load('receipts',op['receipt_id']),receipt_id=op['receipt_id'],
                                receipt_reused=True,current_freshness_verified=False)
                raise ValueError('REVERT_OUTCOME_UNKNOWN: inspect native receipt; never replay automatically')
            _, before = await self._snapshot(board_path=record['context']['board_path'],source='live')
            if capture_digest(before['snapshot']) != receipt['post_capture_digest']:
                raise ValueError('STALE_PLACEMENT: intervening edit or editor reload; revert refused')
            from suite_checkpoints import _atomic
            operation.parent.mkdir(parents=True,exist_ok=True)
            claim=operation.with_name(operation.name+'.'+os.urandom(12).hex()+'.tmp')
            try:
                _atomic(claim,_json(dict(plan_id=plan_id,status='dispatching',kind='in_memory_compensation')))
                try: os.link(claim,operation)
                except FileExistsError as exc: raise ValueError('Concurrent revert already claimed') from exc
            finally: claim.unlink(missing_ok=True)
            args=dict(board_path=record['context']['board_path'],board_identity=before['snapshot']['board_identity'],
                      expected_native_snapshot_token=before['snapshot']['native_snapshot_token'],
                      moves=moves,operation_id=operation_id)
            ticket=self._native_ticket.set((asyncio.current_task(),digest(args)))
            try: native=await self._native('_suite_layout_apply_plan',args)
            finally: self._native_ticket.reset(ticket)
            result=dict(plan_id=plan_id,operation_id=operation_id,reverted=False,applied=False,
                        exact_moves=moves,readback_coords=native.get('readback',[]),native=native,
                        saved=False,altium_closed=False,automatic_retry=False,
                        qualification_scope='original placement poses only; not a full unsaved-buffer undo')
            if native.get('success') is True:
                _, after=await self._snapshot(board_path=record['context']['board_path'],source='live')
                current=context_from_snapshot(before['snapshot'],'before-revert',record.get('models',{}),
                    record.get('fabrication',{}),record.get('assembly',{}),plan['policy_version'])
                errors=unexpected_post_changes(before['snapshot'],after['snapshot'],current,moves)
                result.update(unexpected_post_changes=errors,post_capture_digest=capture_digest(after['snapshot']))
                result['reverted']=not errors
            rid=self._save('receipts',result)
            _atomic(operation,_json(dict(plan_id=plan_id,status='completed',receipt_id=rid)))
            self._scope(plan['context_id'],dict(scope,phase='reverted' if result['reverted'] else 'inspect',
                       gate_passed=False,receipt_id=rid))
            return dict(result,receipt_id=rid)

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
