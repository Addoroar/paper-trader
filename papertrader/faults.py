"""Fault injection used by crash-recovery tests and demos.

Set ``PAPERTRADER_FAULT`` to ``<point>`` or ``<point>:kill`` (optionally
``<point>:kill:N`` / ``<point>:raise:N`` to fire on the N-th hit). With
``raise`` a :class:`SimulatedCrash` is raised; with ``kill`` the process sends
itself SIGKILL, which is indistinguishable from ``kill -9`` or power loss.

Points: ``after_place`` (order row committed, fill not yet applied),
``after_fill`` (fill committed, bar cursor not yet advanced) and
``after_round`` (an account's decision round fully committed).
"""

from __future__ import annotations

import os
import signal


class SimulatedCrash(RuntimeError):
    """Raised at a fault point in ``raise`` mode."""


_hits: dict[str, int] = {}
_override: str | None = None


def set_fault(spec: str | None) -> None:
    """Programmatically set the fault spec (overrides the env var); ``None`` clears it."""
    global _override
    _override = spec
    _hits.clear()


def maybe_fault(point: str) -> None:
    """Trigger the configured fault if ``point`` matches."""
    spec = _override if _override is not None else os.environ.get("PAPERTRADER_FAULT", "")
    if not spec:
        return
    parts = spec.split(":")
    if parts[0] != point:
        return
    mode = parts[1] if len(parts) > 1 else "raise"
    nth = int(parts[2]) if len(parts) > 2 else 1
    _hits[point] = _hits.get(point, 0) + 1
    if _hits[point] != nth:
        return
    if mode == "kill":
        os.kill(os.getpid(), signal.SIGKILL)
    raise SimulatedCrash(f"simulated crash at {point}")
