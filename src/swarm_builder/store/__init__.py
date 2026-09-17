"""Filesystem-backed storage for graph documents and generated projects.

Two independent lifecycles live here, split into their own modules
because they have different failure surfaces:

- :mod:`swarm_builder.store.graphs` -- one JSON file per graph
  (``<workspace>/graphs/<id>.json``), read/written/validated through
  :class:`swarm_builder.models.SwarmGraph`.
- :mod:`swarm_builder.store.projects` -- one directory per generated
  project (``<workspace>/projects/<id>/``), whose *contents* are the
  scaffolder's job, not this package's -- this package only owns the
  directory's lifecycle (create/clear/delete).

Both modules take an explicit ``workspace_dir: Path`` argument on every
function rather than calling :func:`swarm_builder.config.get_workspace_dir`
themselves. That is deliberate: it keeps this package ignorant of where
the *real* workspace lives, which is what makes "point a test at
``tmp_path`` and never touch the real ``workspace/`` directory" trivial
for every caller, including the route layer.

:mod:`swarm_builder.store._ids` is the shared path-safety primitive both
modules build on: every graph/project id that reaches a filesystem path
is validated against a narrow charset and re-checked to resolve inside
its intended root before any read, write, or ``rmtree``.
"""

from __future__ import annotations
