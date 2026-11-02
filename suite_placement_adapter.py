"""MIT. Bounded transport validation for plan-bound native placement only.

Geometry, collision policy and engineering gates belong to the layout core.
This module performs no editor or file operations.
"""
from __future__ import annotations

import math
import ntpath
import re

MAX_MOVES = 500
MM_PER_NATIVE_COORD = 0.00000254
MAX_ABSOLUTE_MM = (2**31 - 2) * MM_PER_NATIVE_COORD
_TOKEN = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
_ADDRESS = re.compile(r'[1-9][0-9]{0,19}\Z')
_MOVE_FIELDS = frozenset(('component_id', 'designator', 'x_mm', 'y_mm', 'rotation_deg', 'side'))


def _number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{field} must be a finite JSON number')
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f'{field} must be a finite JSON number') from exc
    if not math.isfinite(number):
        raise ValueError(f'{field} must be a finite JSON number')
    return number


def _decimal(value):
    # The native locale-safe parser accepts bounded decimal strings. Avoid
    # exponent conversions that could raise an IDE modal before Try/Except.
    text = format(value, '.12f').rstrip('0').rstrip('.')
    return '0' if text in {'', '-0'} else text


def prepare_layout_native_batch(board_path, board_identity, expected_native_snapshot_token,
                                moves, operation_id):
    """Reject an invalid tail before any IPC; preserve sub-mil coordinates."""
    if (not isinstance(board_path, str) or not board_path
            or not ntpath.isabs(board_path) or not board_path.casefold().endswith('.pcbdoc')
            or '\x00' in board_path):
        raise ValueError('An absolute target .PcbDoc path is required')
    if not isinstance(board_identity, str) or not _ADDRESS.fullmatch(board_identity):
        raise ValueError('The native board identity must be a captured object address')
    for name, value in (('snapshot token', expected_native_snapshot_token), ('operation_id', operation_id)):
        if not isinstance(value, str) or not _TOKEN.fullmatch(value):
            raise ValueError(f'Invalid native placement {name}')
    if not isinstance(moves, list) or not 1 <= len(moves) <= MAX_MOVES:
        raise ValueError(f'Placement requires 1..{MAX_MOVES} explicit component moves')
    identifiers, names, packed = set(), set(), []
    for move in moves:
        if not isinstance(move, dict) or set(move) != _MOVE_FIELDS:
            raise ValueError('Each move must contain exactly component_id/designator/x_mm/y_mm/rotation_deg/side')
        identifier, name = move['component_id'], move['designator']
        if not isinstance(identifier, str) or not _ADDRESS.fullmatch(identifier) or identifier in identifiers:
            raise ValueError('Component IDs must be unique captured native object addresses')
        if (not isinstance(name, str) or not name.strip() or len(name) > 256
                or any(ord(char) < 32 for char in name) or name.casefold() in names):
            raise ValueError('Designators must be nonempty and unique')
        if move['side'] not in ('top', 'bottom'):
            raise ValueError('Only explicit top/bottom placement sides are supported')
        x, y = (_number(move[field], field) for field in ('x_mm', 'y_mm'))
        rotation = _number(move['rotation_deg'], 'rotation_deg')
        if abs(x) > MAX_ABSOLUTE_MM or abs(y) > MAX_ABSOLUTE_MM:
            raise ValueError('Placement coordinates exceed the signed native coordinate range')
        if not 0 <= rotation < 360:
            raise ValueError('rotation_deg must be normalized into [0, 360)')
        identifiers.add(identifier)
        names.add(name.casefold())
        packed.append({'component_id': identifier, 'designator': name,
                       'x_mm': _decimal(x), 'y_mm': _decimal(y),
                       'rotation_deg': _decimal(rotation), 'side': move['side']})
    params = {'board_path': board_path, 'board_identity': board_identity,
              'expected_native_snapshot_token': expected_native_snapshot_token,
              'operation_id': operation_id, 'move_count': len(packed)}
    params.update({f'move_{index}': move for index, move in enumerate(packed)})
    return params
