"""Attachment support: reading uploaded files into drafter context.

The package splits by concern, and every module is importable without a
provider credential or a heavy parser at import time:

* :mod:`~swarm_builder.attachments.limits` -- every cap, in one place.
* :mod:`~swarm_builder.attachments.models` -- the record shape, the supported
  formats, and the exceptions whose messages are safe to show a user.
* :mod:`~swarm_builder.attachments.extract` -- per-format readers
  (``openpyxl``/``python-pptx`` are imported inside the functions they serve).
* :mod:`~swarm_builder.attachments.store` -- the TTL'd, path-safe workspace store.
* :mod:`~swarm_builder.attachments.context` -- the bounded prompt block.

Nothing here is part of the graph document: an attachment is evidence for one
description, kept in workspace scratch space and never in ``SwarmGraph``.
"""

from __future__ import annotations

from swarm_builder.attachments import context, extract, limits, models, store

__all__ = ["context", "extract", "limits", "models", "store"]
