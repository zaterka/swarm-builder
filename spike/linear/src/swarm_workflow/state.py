"""Graph state for the linear spike workflow.

Owned exclusively by graph.py; steps interact with it via ``ctx.state``.
Not a permitted file for model edits in the real emitter (see PLAN.md
Phase 3), reproduced here for shape parity with the target scaffold.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class State:
    """Fields threaded through the linear workflow."""

    topic: str = ""
    notes: str = ""
