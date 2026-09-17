"""Compile subpackage: deterministic codegen (Group 2) lives here.

This module (``compile/__init__.py``) is deliberately a **neutral leaf**:
it defines the seam between ``scaffold.py`` (Group 2, owned here) and
Group 3's concurrently-developed ``inherit/routes.py``, so neither module
imports the other. ``scaffold.py`` imports :class:`ResolvedModel` and the
marker-formatting helpers from here; Group 3's ``inherit/routes.py`` will
construct a :class:`ResolvedModel` and hand it to the compile pipeline
without ever importing ``scaffold.py`` or vice versa.

Also home to the cross-group marker-region contract (PLAN.md Phase 3):
the exact ``# --- swarm:imports <nodeId> ---`` / ``# --- swarm:begin/end
<nodeId> ---`` text Group 4's ``boundary.py`` will need to parse. Defined
as functions, not inline literals in templates, so there is exactly one
place that spells them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResolvedModel:
    """The narrow interface ``scaffold.py`` needs from route resolution.

    Owned by Group 2 (this module), constructed by whoever resolves a
    harness route to a PydanticAI model -- Group 3's ``inherit/routes.py``
    in the real pipeline, or :func:`default_scaffold_model` here for
    Group 2's own tests, which must not depend on Group 3 existing yet.

    Every field is a **pre-rendered source fragment**, never a
    provider/protocol enum -- ``scaffold.py`` only splices strings and
    never branches on "is this bedrock or openai", which is what keeps
    the two groups decoupled (facts 22-23 stay entirely on the
    producing side of this seam).
    """

    #: Literal multi-line Python source: any constants the resolver
    #: needs (``DEFAULT_MODEL`` / ``DEFAULT_MODEL_ID`` / ``DEFAULT_BASE_URL``
    #: etc.) plus exactly one module-level function definition named by
    #: :attr:`default_factory_name` that returns a ``Model | str`` and
    #: ALREADY implements the ``SWARM_MODEL`` / ``SWARM_BASE_URL`` /
    #: ``SWARM_API_KEY_ENV`` env-override convention (PLAN.md "Model
    #: inheritance"). Mirrors ``spike/linear/deps.py`` or
    #: ``spike/linear_custom_baseurl/deps.py`` minus the ``Deps``
    #: dataclass itself -- ``scaffold.py`` emits that part uniformly.
    helper_source: str

    #: Name of the function ``helper_source`` defines, used as
    #: ``field(default_factory=<default_factory_name>)`` in the emitted
    #: ``Deps`` dataclass.
    default_factory_name: str = "_resolve_default_model"

    #: Extra import lines ``deps.py`` needs above ``helper_source``
    #: (e.g. ``("from pydantic_ai.models.openai import OpenAIChatModel",
    #: "from pydantic_ai.providers.openai import OpenAIProvider")``).
    extra_imports: tuple[str, ...] = ()

    #: Bracketed ``pydantic-ai-slim[...]`` extras ONLY (fact 23), e.g.
    #: ``("bedrock",)``. Never a bare package name -- those are template
    #: deps or programmatic ``needs``, unioned separately in
    #: ``scaffold.py``.
    pyproject_extras: tuple[str, ...] = ()

    #: Lines appended verbatim to the generated project's
    #: ``.env.example``, naming the route the project inherited so a
    #: reader can change it without returning to the canvas. A known-name
    #: route contributes ``SWARM_MODEL``; a custom-``baseURL`` route also
    #: contributes ``SWARM_BASE_URL`` and ``SWARM_API_KEY_ENV``. Empty
    #: only for the test-fixture model.
    env_lines: tuple[str, ...] = ()

    #: One line describing the inherited route, spliced into the
    #: generated ``README.md``.
    readme_model_note: str = ""


def default_scaffold_model() -> ResolvedModel:
    """The known-name fallback path (fact 22, row 1), mirroring
    ``spike/linear/src/swarm_workflow/deps.py`` exactly.

    **Test fixture only — never wire this into the real pipeline.** It
    exists so Group 2's codegen tests never depend on Group 3's
    ``inherit/routes.py``. The production path is always
    ``inherit.settings.resolve_effective_model()`` →
    ``inherit.routes.to_resolved_model()``, whose no-route fallback is
    the bundle default PLAN.md mandates (``deepseek-official`` /
    ``deepseek-v4-flash``) reported with its ``source``, NOT the
    bedrock known-name string hard-coded below. Using this function in
    the pipeline would silently route a compile to a model the user
    never configured, which is precisely the I5 failure the "report the
    source" requirement exists to prevent.
    """
    helper_source = (
        'DEFAULT_MODEL: str = "bedrock:us.anthropic.claude-opus-5"\n'
        "\n"
        "\n"
        "def _resolve_default_model() -> str:\n"
        '    return os.environ.get("SWARM_MODEL", DEFAULT_MODEL)\n'
    )
    return ResolvedModel(
        helper_source=helper_source,
        default_factory_name="_resolve_default_model",
        extra_imports=(),
        pyproject_extras=("bedrock",),
        env_lines=(),
        readme_model_note=(
            "Inherited default model: bedrock:us.anthropic.claude-opus-5 "
            "(override with SWARM_MODEL)."
        ),
    )


# ---------------------------------------------------------------------------
# Marker-region contract (PLAN.md Phase 3) -- the single source of truth
# for the exact marker text Group 4's boundary.py must parse. Body markers
# are indented one level (inside a function body); a parser must match
# ignoring leading whitespace.
# ---------------------------------------------------------------------------


def imports_marker_begin(node_id: str) -> str:
    """Return the opening marker of a module's top-of-file imports region.

    The region exists because a filled body that needs ``import httpx``
    has nowhere else to put the import: the module's own import block is
    written by the scaffolder, and an import inside the body markers would
    be visible to the agent but not to the module's header.
    """
    return f"# --- swarm:imports {node_id} ---"


def imports_marker_end(node_id: str) -> str:
    """Return the closing marker of a module's imports region."""
    return f"# --- swarm:end-imports {node_id} ---"


def body_marker_begin(node_id: str) -> str:
    """Return the opening marker of a step function's body region.

    Emitted indented one level inside the function body, so a parser must
    match this text ignoring leading whitespace.
    """
    return f"# --- swarm:begin {node_id} ---"


def body_marker_end(node_id: str) -> str:
    """Return the closing marker of a step function's body region."""
    return f"# --- swarm:end {node_id} ---"


__all__ = [
    "ResolvedModel",
    "body_marker_begin",
    "body_marker_end",
    "default_scaffold_model",
    "imports_marker_begin",
    "imports_marker_end",
]
