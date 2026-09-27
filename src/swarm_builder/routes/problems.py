"""One error-detail shape for the routes that report structured problems.

Most routes in this app raise ``HTTPException`` with a plain string detail, which
is all a single-purpose failure needs. The generate/attachment family needs more:
the client has to tell "this attachment expired" from "this model cannot read
images" without inspecting prose, and it already renders a list of ``problems``.

Hence one shape, built in one place::

    {"detail": {"code": "attachment_expired", "message": "...", "problems": []}}

``code`` is a stable machine-readable string, ``message`` is the sentence to
show, ``problems`` is the (possibly empty) list of individual findings. It is a
strict superset of the ``{message, problems}`` detail the generate endpoint
already returned, so an existing client keeps working.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import HTTPException


def problem(
    status_code: int,
    code: str,
    message: str,
    problems: Sequence[str] = (),
) -> HTTPException:
    """Build an ``HTTPException`` carrying the structured detail shape.

    Args:
        status_code: The HTTP status to return.
        code: A stable identifier for the failure, for client-side branching.
        message: The single sentence to show the user.
        problems: Individual findings, when there is more than one.

    Returns:
        The exception to raise from a route handler.
    """
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "problems": list(problems)},
    )


__all__ = ["problem"]
