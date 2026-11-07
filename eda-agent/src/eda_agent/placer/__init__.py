# SPDX-License-Identifier: MIT
"""Electronics AI professional placement engine (EDA-neutral core).

Layers:
    ir / geometry / sexpr  -- board model and primitives
    io                     -- KiCad and Altium adapters
    metrics                -- routability proxies and organisation scores
    render                 -- SVG / HTML review output
    route_check            -- trial routing with Freerouting
"""

from .ir import Board, Footprint, Pad, Rules

__all__ = ["Board", "Footprint", "Pad", "Rules"]
