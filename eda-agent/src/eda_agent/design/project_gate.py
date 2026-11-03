# SPDX-License-Identifier: MIT
"""Read actual project artifacts and declared isolation inputs without defaults."""
import hashlib
import json
import math
from pathlib import Path

UNKNOWN = {'', 'tbd', 'unknown', 'open', 'pending', 'not assessed', 'not yet assessed'}
SAFETY_FIELDS = ('working_voltage', 'insulation_type', 'transient', 'test_voltage_and_time',
                 'creepage', 'clearance', 'standard_and_revision')


def evaluate_project_gate(project_dir, safety_requirements):
    root = Path(project_dir).resolve()
    if not Path(project_dir).is_absolute():
        raise ValueError('An explicit absolute project directory is required')
    if not isinstance(safety_requirements, dict) or not isinstance(safety_requirements.get('isolation_required'), bool):
        raise ValueError('Explicit isolation_required boolean is required')
    boards = sorted(p for p in root.rglob('*') if p.is_file() and p.suffix.casefold()=='.pcbdoc'
                    and '.git' not in p.relative_to(root).parts) if root.is_dir() else []
    missing = []
    def known(value):
        if value is None or isinstance(value, bool): return False
        if isinstance(value, (int,float)): return math.isfinite(value) and value>0
        return isinstance(value,str) and value.strip().casefold() not in UNKNOWN
    if safety_requirements['isolation_required']:
        barriers = safety_requirements.get('barriers')
        if not isinstance(barriers,list) or not barriers:
            missing.append('barriers')
        else:
            for index, barrier in enumerate(barriers):
                if not isinstance(barrier,dict):
                    missing.append(f'barriers[{index}]'); continue
                for field in SAFETY_FIELDS:
                    if not known(barrier.get(field)):
                        missing.append(f'barriers[{index}].{field}')
    blockers = []
    if not boards: blockers.append('blocked_missing_board_artifact')
    if len(boards)>1: blockers.append('blocked_ambiguous_board_artifact')
    if missing: blockers.append('blocked_missing_safety_input')
    report = {'schema':1, 'project_dir':str(root), 'directory_exists':root.is_dir(),
        'board_artifacts':[{'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in boards],
        'target_board_path':str(boards[0]) if len(boards)==1 else None,
        'requirements':safety_requirements, 'missing_safety_inputs':missing,
        'blockers':blockers, 'status':blockers[0] if blockers else 'inputs_ready_for_placement_review',
        'passed':not blockers, 'placement_qualified':False,
        'limits':'Input readiness only; no safety compliance, insulation design or PCB placement approval'}
    report['evidence_id'] = hashlib.sha256(json.dumps(report,sort_keys=True,allow_nan=False).encode()).hexdigest()
    return report
