"""HTTP route modules.

Each sibling module in this package exports a module-level
``router: APIRouter`` that :mod:`swarm_builder.main` includes under the
``/api`` prefix. Keeping one ``APIRouter`` per concern (health, graphs,
templates, models, export, compile) is the idiomatic FastAPI composition
pattern, and it keeps ``main.py`` a thin assembly file rather than a place
where routes accumulate directly.

**Load-bearing rule, repeated in every handler's own docstring below:**
every environment and settings lookup (``swarm_builder.config.get_*``,
``swarm_builder.inherit.settings.read_settings``,
``resolve_effective_model``) is called FRESH inside the request handler
that needs it -- never cached on ``app.state``, never memoized at module
import time, never stored in a module-level global. Both ``settings.yaml``
and every environment variable this app reads are hot-reloadable while the
server is running (a developer editing ``settings.yaml`` between two
requests must see the new value on the very next request), and a cached
snapshot would silently defeat that.
"""

from __future__ import annotations
