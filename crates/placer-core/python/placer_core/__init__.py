# SPDX-License-Identifier: MIT
"""Electronics AI placement core (Rust): geometry, incremental cost, legalization, annealing."""

from ._core import PlacementProblem, __version__, hpwl

__all__ = ["PlacementProblem", "__version__", "hpwl"]
