# SPDX-License-Identifier: MIT
"""F0b acceptance: semantic KiCad round-trip on the 8 corpus boards (slow; ELECTRONICS_AI_SLOW=1).

interf_u is sealed; this I/O-only check is the one use the plan allows before test #1.
"""

from __future__ import annotations

import os

import pytest
from eda_agent.placer import corpus
from eda_agent.placer.io.roundtrip import roundtrip

pytestmark = pytest.mark.skipif(os.environ.get("ELECTRONICS_AI_SLOW") != "1",
                                reason="slow KiCad round-trip; set ELECTRONICS_AI_SLOW=1")


@pytest.mark.parametrize("name", corpus.ROUNDTRIP_SET)
def test_roundtrip_is_semantically_unchanged(name, tmp_path):
    entry = corpus.get(name, io_only=True)
    result = roundtrip(entry.path, tmp_path)
    assert result["identity_diffs"] == []
    assert result["restored_diffs"] == []
