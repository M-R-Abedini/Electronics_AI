# SPDX-License-Identifier: MIT
"""Minimal, position-preserving S-expression reader for KiCad files.

Every list node remembers the character span it occupies in the source text so
writers can patch individual nodes (for example a footprint ``(at ...)``)
without re-serialising, and therefore without reformatting, the whole file.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

_TOKEN = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')
_UNESCAPE = re.compile(r"\\(.)")


class Node(list):
    """A list node with its ``[start, end)`` span in the source text."""

    __slots__ = ("end", "start")

    def __init__(self, start: int = 0) -> None:
        super().__init__()
        self.start = start
        self.end = start

    @property
    def tag(self) -> str | None:
        return self[0] if self and isinstance(self[0], str) else None

    def children(self, tag: str | None = None) -> Iterator[Node]:
        for item in self:
            if isinstance(item, Node) and (tag is None or item.tag == tag):
                yield item

    def first(self, tag: str) -> Node | None:
        return next(self.children(tag), None)

    def value(self, tag: str, index: int = 1, default=None):
        node = self.first(tag)
        if node is None or len(node) <= index:
            return default
        return node[index]

    def floats(self, tag: str) -> list[float] | None:
        node = self.first(tag)
        if node is None:
            return None
        out = []
        for item in node[1:]:
            if isinstance(item, str):
                try:
                    out.append(float(item))
                except ValueError:
                    break
        return out


class Text(str):
    """A quoted string atom (distinguishable from bare symbols)."""

    __slots__ = ()


def parse(text: str) -> Node:
    """Parse ``text`` and return the single top-level list node."""
    stack: list[Node] = []
    root: Node | None = None
    for match in _TOKEN.finditer(text):
        token = match.group(0)
        if token == "(":
            node = Node(match.start())
            if stack:
                stack[-1].append(node)
            stack.append(node)
        elif token == ")":
            if not stack:
                raise ValueError(f"Unbalanced ')' at offset {match.start()}")
            node = stack.pop()
            node.end = match.end()
            if not stack:
                root = node
                break
        elif not stack:
            raise ValueError(f"Atom outside a list at offset {match.start()}")
        elif token[0] == '"':
            stack[-1].append(Text(_UNESCAPE.sub(r"\1", token[1:-1])))
        else:
            stack[-1].append(token)
    if root is None or stack:
        raise ValueError("Unterminated S-expression")
    return root


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def fmt(value: float) -> str:
    """Format a number the way KiCad writes it (no trailing zeros)."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text
