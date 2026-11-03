# SPDX-License-Identifier: MIT
"""Explicit request-local PCB targets shared by full and lean MCP registration."""
from contextvars import ContextVar
from functools import wraps
import inspect
from pathlib import PureWindowsPath
from typing import get_type_hints

TARGET = ContextVar('explicit_altium_board_target', default=None)
ERROR = 'ERR_TARGET_DOCUMENT_MISMATCH'


def bind_target(params):
    params = dict(params)
    target = TARGET.get()
    aliases = [p for p in (target, params.get('target_board_path'), params.get('board_path')) if p]
    if aliases and (any(not isinstance(p, str) or not PureWindowsPath(p).is_absolute() for p in aliases)
                    or len({str(PureWindowsPath(p)).casefold() for p in aliases}) != 1):
        raise ValueError(ERROR + ': target aliases must name one absolute PCB')
    if target:
        params['target_board_path'] = target
    return params


def targeted_tool(fn):
    if getattr(fn, '_pcb_target_wrapper', False):
        return fn
    signature = inspect.signature(fn)
    if 'target_board_path' in signature.parameters:
        return fn
    hints = get_type_hints(fn)
    @wraps(fn)
    async def wrapped(*args, target_board_path=None, **kwargs):
        inherited = TARGET.get()
        if target_board_path and inherited and str(PureWindowsPath(target_board_path)).casefold() != str(PureWindowsPath(inherited)).casefold():
            raise ValueError(ERROR + ': nested invocation changes the bound target')
        token = TARGET.set(target_board_path or inherited)
        try:
            result = fn(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result
        finally:
            TARGET.reset(token)
    parameters = [p.replace(annotation=hints.get(p.name, p.annotation)) for p in signature.parameters.values()]
    parameters.append(inspect.Parameter('target_board_path', inspect.Parameter.KEYWORD_ONLY,
        default=None, annotation=str | None))
    wrapped.__signature__ = signature.replace(parameters=parameters, return_annotation=hints.get('return', signature.return_annotation))
    wrapped.__annotations__ = {**hints, 'target_board_path': str | None}
    wrapped._pcb_target_wrapper = True
    return wrapped


class TargetedRegistration:
    """Preserve every tool name; make an explicit target discoverable in schema."""
    def __init__(self, delegate):
        self.delegate = delegate
    def tool(self, *args, **kwargs):
        def register(fn):
            # Pure engineering calculators and plan-only design tools retain
            # their cross-backend schemas; they do not select a native PCB.
            selected = fn if fn.__name__.startswith(('pcb_calc_', 'design_')) else targeted_tool(fn)
            return self.delegate.tool(*args, **kwargs)(selected)
        return register
    def __getattr__(self, name):
        return getattr(self.delegate, name)
