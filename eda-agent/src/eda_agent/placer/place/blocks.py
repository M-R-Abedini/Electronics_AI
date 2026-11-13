# SPDX-License-Identifier: MIT
"""Seed electrical blocks using pad affinities before placing independent parts."""

from __future__ import annotations

import math

from ..geometry import rotated_box
from ..intent import model
from .problem import PlaceProblem, _rot90


def seed(pb: PlaceProblem, intent: dict, start: list[tuple[float, float, int]]) -> tuple[list, list[int], int]:
    poses = list(start)
    by_ref = {ref: i for i, ref in enumerate(pb.refs)}
    links: dict[int, list] = {}
    for pins, weight in pb.affinities:
        links.setdefault(pb.pins[pins[0]][0], []).append((pins, weight))
    owners = {}
    anchors = set()
    for claim in intent.get("blocks", []):
        if model.is_uncertain(claim):
            continue
        block = claim["value"]
        root = by_ref.get(block.get("anchor"))
        if root is None:
            continue
        anchors.add(root)
        for ref in block["members"]:
            index = by_ref.get(ref)
            if index is not None and index != root and index in links and not pb.parts[index][5]:
                owners.setdefault(index, set()).add(root)

    def pad_at(pin, pose):
        _, lx, ly = pb.pins[pin]
        x, y, rotation = pose
        dx, dy = _rot90(lx, ly, rotation)
        return x + dx, y + dy

    placed = set(anchors) | {i for i, part in enumerate(pb.parts) if part[5]}
    ordered = []
    pending = {index for index, roots in owners.items() if len(roots) == 1 and index not in placed}
    while pending:
        ready = [i for i in sorted(pending) if all(pb.pins[p][0] in placed for group, _ in links[i] for p in group[1:])]
        if not ready:
            break
        for i in ready:
            group = links[i]
            _, *target_pins = group[0][0]
            candidates = []
            for pin in target_pins:
                root = pb.pins[pin][0]
                x, y = pad_at(pin, poses[root])
                target_box = rotated_box(pb.parts[root][:4], poses[root][:2], poses[root][2] * 90)
                for r in range(4):
                    a, b, c, d = rotated_box(pb.parts[i][:4], (0, 0), r * 90)
                    # Seed beside the target's box, aligning the actual source and target pads.
                    sx, sy = pad_at(group[0][0][0], (0, 0, r))
                    candidates.extend((px, py, r) for px, py in ((target_box[0] - c - 0.2, y - sy),
                        (target_box[2] - a + 0.2, y - sy), (x - sx, target_box[1] - d - 0.2),
                        (x - sx, target_box[3] - b + 0.2)))

            def cost(pose, group=group):
                return sum(weight * min(math.dist(pad_at(pins[0], pose), pad_at(pin, poses[pb.pins[pin][0]]))
                                        for pin in pins[1:]) for pins, weight in group)

            poses[i] = min(candidates, key=cost)
            placed.add(i)
            pending.remove(i)
            ordered.append(i)
    return poses, sorted(anchors | set(ordered)), len(ordered)
