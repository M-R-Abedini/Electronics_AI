"""The native ANSI boundary validates labels before publishing requests."""
import pytest
from suite_json_guard import validate_native_json_text

@pytest.mark.parametrize('payload', [{'label': 'Ω'}, {'pins': ['你好']}, {'فارسی': 'R1'}])
def test_unsupported_native_text_is_rejected(payload):
    with pytest.raises(ValueError, match='Latin-1'):
        validate_native_json_text(payload)

def test_native_punctuation_escapes_and_latin1_supported():
    validate_native_json_text({'label': '10k, "precision"', 'notes': 'line\nnext\tfield', 'unit': 'µ'})
