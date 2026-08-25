from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable
import numpy as np


@dataclass
class RecoveryStep:
    start: int
    end: int
    payload: Any

    def to_dict(self):
        payload = (
            self.payload.to_dict()
            if hasattr(self.payload, "to_dict")
            else self.payload
        )
        return {
            "start": int(self.start),
            "end": int(self.end),
            "payload": payload,
        }


@dataclass
class RecoveryResult:
    success: bool
    steps: list[RecoveryStep] = field(default_factory=list)
    attempts: int = 0
    max_depth_reached: int = 0
    tried_intervals: list[tuple[int, int]] = field(default_factory=list)
    reason: str = ""

    def to_dict(self):
        return {
            "success": bool(self.success),
            "steps": [x.to_dict() for x in self.steps],
            "attempts": int(self.attempts),
            "max_depth_reached": int(self.max_depth_reached),
            "tried_intervals": [
                [int(a), int(b)] for a, b in self.tried_intervals
            ],
            "reason": self.reason,
        }


def ordered_split_candidates(
    start: int,
    end: int,
    max_candidates: int = 0,
) -> list[int]:
    if end - start <= 1:
        return []

    points = list(range(start + 1, end))
    mid = (start + end) / 2.0
    points.sort(key=lambda x: (abs(x - mid), x))

    if max_candidates and max_candidates > 0:
        points = points[: int(max_candidates)]

    return points


def compose_recovery_steps(steps: list[RecoveryStep]) -> np.ndarray:
    """
    Compose chronological source->target transforms.

    A->B, B->C, C->D:
      T_A_D = T_C_D @ T_B_C @ T_A_B
    """
    T = np.eye(4, dtype=float)
    for step in steps:
        T = np.asarray(step.payload.transformation, dtype=float) @ T
    return T


def recover_interval(
    start: int,
    end: int,
    solve_pair: Callable[[int, int], Any | None],
    *,
    max_depth: int = 5,
    max_attempts: int = 192,
    max_split_candidates: int = 0,
    skip_direct_root: bool = True,
) -> RecoveryResult:
    if end <= start:
        return RecoveryResult(
            success=False,
            reason=f"invalid_interval:{start}->{end}",
        )

    state = {
        "attempts": 0,
        "max_depth_reached": 0,
        "tried": [],
        "budget_exhausted": False,
    }

    memo: dict[tuple[int, int, int], list[RecoveryStep] | None] = {}

    def try_pair(a: int, b: int):
        if state["attempts"] >= max_attempts:
            state["budget_exhausted"] = True
            return None

        state["attempts"] += 1
        state["tried"].append((a, b))

        payload = solve_pair(a, b)
        if payload is None:
            return None

        return [RecoveryStep(a, b, payload)]

    def rec(a: int, b: int, depth: int, skip_direct: bool = False):
        state["max_depth_reached"] = max(
            state["max_depth_reached"],
            depth,
        )

        remaining_depth = max_depth - depth
        key = (a, b, remaining_depth)

        if key in memo:
            cached = memo[key]
            return None if cached is None else list(cached)

        if state["budget_exhausted"]:
            memo[key] = None
            return None

        if not skip_direct:
            solved = try_pair(a, b)
            if solved is not None:
                memo[key] = solved
                return list(solved)

        if b - a <= 1:
            memo[key] = None
            return None

        if depth >= max_depth:
            memo[key] = None
            return None

        for mid in ordered_split_candidates(
            a,
            b,
            max_candidates=max_split_candidates,
        ):
            if state["budget_exhausted"]:
                break

            left = rec(a, mid, depth + 1)
            if left is None:
                continue

            right = rec(mid, b, depth + 1)
            if right is None:
                continue

            chain = left + right
            memo[key] = chain
            return list(chain)

        memo[key] = None
        return None

    chain = rec(
        int(start),
        int(end),
        depth=0,
        skip_direct=bool(skip_direct_root),
    )

    if chain is not None:
        return RecoveryResult(
            success=True,
            steps=chain,
            attempts=state["attempts"],
            max_depth_reached=state["max_depth_reached"],
            tried_intervals=state["tried"],
            reason="recovered",
        )

    if state["budget_exhausted"]:
        reason = "max_attempts_exhausted"
    elif state["max_depth_reached"] >= max_depth:
        reason = "max_depth_or_adjacent_failure"
    else:
        reason = "no_valid_subdivision_path"

    return RecoveryResult(
        success=False,
        steps=[],
        attempts=state["attempts"],
        max_depth_reached=state["max_depth_reached"],
        tried_intervals=state["tried"],
        reason=reason,
    )