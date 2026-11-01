"""Versioned, millimetre copper evidence shared by native and file readers.

SPDX-License-Identifier: MIT
No geometry, dielectric constant, or connectivity is inferred by this model.
An incomplete snapshot is useful evidence, but cannot certify a clean board.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import ntpath

SECTIONS = ('layers', 'tracks', 'arcs', 'vias', 'pads', 'regions', 'keepouts',
            'holes', 'cutouts', 'net_ties', 'components', 'nets', 'unsupported_regions')


def same_board_path(left: str, right: str) -> bool:
    """Altium paths are Windows paths, even when tests run on another OS."""
    return bool(left and right) and ntpath.normcase(ntpath.normpath(left)) == ntpath.normcase(ntpath.normpath(right))


def empty_snapshot(board_path='', *, source='unavailable', missing=()):
    result = dict(schema=1, units='mm', board_path=str(board_path), source=source,
                  complete=False, missing=list(missing), coverage={},
                  provenance={'reader': 'copper_snapshot', 'live': source == 'live'})
    result.update({name: [] for name in SECTIONS})
    return result


def validate_snapshot(value: dict, *, expected_board_path: str | None = None) -> dict:
    """Validate wire evidence, returning it unchanged; reject nonfinite geometry.

    Readers must report omissions, not replace unreadable fields with plausible
    defaults. Additional metadata is allowed for forward-compatible provenance.
    """
    if not isinstance(value, dict) or value.get('schema') != 1 or value.get('units') != 'mm':
        raise ValueError('Expected BoardCopperSnapshot schema 1 in mm')
    if not isinstance(value.get('board_path'), str) or not isinstance(value.get('source'), str):
        raise ValueError('Snapshot board_path and source are required')
    if expected_board_path and not same_board_path(expected_board_path, value['board_path']):
        raise ValueError('Copper snapshot target board identity mismatch')
    if type(value.get('complete')) is not bool or not isinstance(value.get('missing'), list):
        raise ValueError('Snapshot completeness and missing evidence are required')
    if any(not isinstance(x, str) or not x for x in value['missing']):
        raise ValueError('Snapshot missing entries must be nonempty strings')
    if value['complete'] and value['missing']:
        raise ValueError('Complete snapshot cannot contain missing evidence')
    required = ('layers', 'tracks', 'arcs', 'vias', 'pads', 'regions', 'keepouts', 'net_ties', 'components', 'nets')
    for section in required:
        if not isinstance(value.get(section), list):
            raise ValueError('Missing snapshot section: ' + section)
    if any(not isinstance(n, str) for n in value['nets']):
        raise ValueError('Snapshot nets must contain names')

    def finite(v, path):
        if type(v) not in (int, float) or not math.isfinite(v):
            raise ValueError('Invalid numeric geometry: ' + path)

    def point(v, path):
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            raise ValueError('Invalid coordinate pair: ' + path)
        for x in v:
            finite(x, path)

    numeric = ('x_mm', 'y_mm', 'width_mm', 'height_mm', 'diameter_mm', 'drill_mm',
               'radius_mm', 'rotation_deg', 'start_angle_deg', 'end_angle_deg',
               'thickness_mm', 'er', 'corner_radius_mm', 'hole_width_mm',
               'hole_rotation_deg', 'hole_slot_length_mm')
    for section in SECTIONS:
        if section == 'nets':
            continue
        if not isinstance(value.get(section, []), list):
            raise ValueError('Invalid snapshot section: ' + section)
        for row in value.get(section, []):
            if not isinstance(row, dict):
                raise ValueError('Invalid snapshot record: ' + section)
            if value['complete'] and row.get('geometry_complete') is False:
                raise ValueError('Complete snapshot contains incomplete geometry: ' + section)
            for key in numeric:
                if key in row and row[key] is not None:
                    finite(row[key], section + '.' + key)
            for key in ('start', 'end', 'center'):
                if key in row:
                    point(row[key], section + '.' + key)
            for p in row.get('outline', []):
                point(p, section + '.outline')
            for hole in row.get('holes', []):
                for p in hole:
                    point(p, section + '.holes')
    for p in value.get('board_outline', []):
        point(p, 'board_outline')
    return value


class BoardCopperSnapshot(dict):
    """Dictionary wire contract with an explicit validated construction path."""

    @classmethod
    def from_dict(cls, value, *, expected_board_path=None):
        return cls(copy.deepcopy(validate_snapshot(value, expected_board_path=expected_board_path)))

    def to_dict(self):
        return copy.deepcopy(dict(self))

    def fingerprint(self):
        return content_fingerprint(self)


def content_fingerprint(snapshot: dict) -> str:
    """Hash physical evidence and gaps, independent of capture time/object IDs.

    Links are replaced with their target's physical content, so remapping native
    object addresses or changing iterator order does not invalidate a preview.
    Source eligibility (live versus saved file) remains a separate policy check.
    """
    validate_snapshot(snapshot)
    ephemeral = {'id', 'owner_id', 'pad_ids', 'polygon_id', 'board_identity',
                 'source', 'provenance', 'captured_at', 'capture_started_at',
                 'snapshot_id', 'fingerprint', 'native_snapshot_json'}

    def encoded(value):
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)

    def physical(value):
        if isinstance(value, dict):
            return {k: physical(v) for k, v in sorted(value.items()) if k not in ephemeral}
        if isinstance(value, (list, tuple)):
            return [physical(v) for v in value]
        return value

    identities = {}
    for section in SECTIONS:
        for row in snapshot.get(section, []):
            if isinstance(row, dict) and row.get('id'):
                identities[row['id']] = hashlib.sha256(encoded(physical(row)).encode()).hexdigest()
    stable = {'schema': 1, 'units': 'mm', 'complete': snapshot['complete'],
              'board_path': ntpath.normcase(ntpath.normpath(snapshot['board_path'])),
              'missing': sorted(set(snapshot['missing'])),
              'coverage': physical(snapshot.get('coverage', {}))}
    for section in SECTIONS:
        rows = []
        for row in snapshot.get(section, []):
            value = physical(row)
            if isinstance(row, dict):
                if row.get('owner_id'):
                    value['owner'] = identities.get(row['owner_id'], 'unresolved-owner')
                if row.get('pad_ids'):
                    value['pads'] = sorted(identities.get(i, 'unresolved-pad') for i in row['pad_ids'])
                if isinstance(value.get('net_names'), list):
                    value['net_names'] = sorted(value['net_names'])
            rows.append(value)
        stable[section] = sorted(rows, key=encoded)
    stable['board_outline'] = physical(snapshot.get('board_outline', []))
    return hashlib.sha256(encoded(stable).encode('utf-8')).hexdigest()
