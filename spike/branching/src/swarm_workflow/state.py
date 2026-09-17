"""Graph state for the branching spike workflow."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class State:
    length_bucket: str = ""
