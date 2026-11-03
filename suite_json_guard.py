"""Validate text for the native DelphiScript ANSI scalar decoder.
Adapted from batch_safety.py, Copyright (c) 2025 Siddharth Ahuja.
SPDX-License-Identifier: MIT
"""
def validate_native_json_text(value, field='input'):
    """Reject text the DelphiScript ANSI scalar decoder cannot represent."""
    if isinstance(value, str):
        try:
            value.encode('latin-1', errors='strict')
        except UnicodeEncodeError as error:
            raise ValueError(f'{field}: text outside Latin-1 is unsupported by the native bridge; no command was published') from error
    elif isinstance(value, dict):
        for key, item in value.items():
            validate_native_json_text(key, f'{field}.key')
            validate_native_json_text(item, f'{field}.{key}')
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            validate_native_json_text(item, f'{field}[{index}]')
