"""Model-route inheritance.

This package resolves which LLM provider/model Swarm Builder's compile
agent should spend, by reading the Factored Harness's user-editable
``settings.yaml`` (facts 3-4, 21 in ``PLAN.md``) and falling back, in a
documented order, to explicit environment configuration and finally to a
bundle default. See :mod:`swarm_builder.inherit.settings` for the actual
parsing and resolution logic; this ``__init__`` intentionally carries no
code so that importing the package has no side effects.
"""

from __future__ import annotations
