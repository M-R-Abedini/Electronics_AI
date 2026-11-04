"""Conservative native effects for an explicitly managed placement scope.

SPDX-License-Identifier: MIT
"""
from __future__ import annotations
import json


def native_effects(backend, name, arguments, *, depth=0):
    if depth > 8 or not isinstance(arguments, dict):
        return {'opaque'}
    if name == 'tool_invoke':
        nested = arguments.get('arguments', arguments.get('args', {}))
        if isinstance(nested, str):
            try: nested = json.loads(nested)
            except ValueError: return {'opaque'}
        return native_effects(backend, str(arguments.get('name', '')), nested, depth=depth + 1)
    if name in {'_suite_copper_snapshot', '_suite_copper_focus', 'app_ping', 'app_context',
                'app_list_open_dialogs', 'pcb_run_drc',
                'obj_select', 'obj_deselect_all', 'obj_zoom'}:
        # These exact handlers change selection/viewport only. Generic modify
        # and process dispatch remain opaque even if a caller claims to focus.
        return set()
    if name == 'app_press_dialog_button':
        return {'opaque'}  # Only the gateway's freshly validated recovery path is exempt.
    if any(word in name for word in ('script', 'run_process', 'execute_menu', 'click_menu', 'ui_command')):
        return {'opaque'}
    if name == '_suite_layout_apply_plan':
        return {'placement'}
    if 'rule' in name and any(word in name for word in ('create', 'delete', 'modify', 'apply', 'set')):
        return {'rules'}
    if 'stackup' in name or 'modify_layer' in name:
        return {'rules'}
    if 'plan_placement' in name:
        return {'placement'} if arguments.get('apply') else set()
    if any(word in name for word in ('move', 'rotate', 'flip', 'place_component', 'autoplace')):
        return {'placement'}
    if any(word in name for word in ('track', 'via', 'route', 'polygon', 'region')) and any(
            word in name for word in ('place', 'add', 'create', 'import', 'delete', 'modify', 'apply', 'route_net')):
        return {'geometry'}
    if any(word in name for word in ('modify_object', 'create_object', 'delete_object', 'batch')):
        return {'geometry', 'opaque'}
    words = name.split('_')
    if any(word in {'get', 'list', 'query', 'check', 'audit', 'inspect', 'calc', 'render', 'snapshot'} for word in words[:3]):
        return set()
    if name.startswith(('design_session_', 'design_next_', 'design_autonomy_')):
        return set()  # Advisory journal writes confer no native permission.
    return {'opaque'}
