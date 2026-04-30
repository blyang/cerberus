"""ETA computation.

Uses persisted average runtime per check (rolling window of 20 from store).
For checks with no history, uses default_estimate_ms from the CheckSpec.
"""
from __future__ import annotations

from . import store
from .checks.base import CheckSpec


class EtaTracker:
    def __init__(self) -> None:
        self.history: dict[str, float] = {}

    def load(self) -> None:
        self.history = store.get_runtime_averages()

    def estimate_remaining_ms(self, specs: list[CheckSpec], completed_ids: set[str]) -> int:
        total = 0
        for spec in specs:
            if spec.id in completed_ids:
                continue
            total += int(self.history.get(spec.id, spec.default_estimate_ms))
        return total

    def record(self, check_id: str, runtime_ms: int) -> None:
        store.record_runtime(check_id, runtime_ms)
        # Update local rolling avg cheaply.
        self.history[check_id] = (self.history.get(check_id, runtime_ms) + runtime_ms) / 2
