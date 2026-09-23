"""Runtime state shared by the routes: the dry-run switch and secret publication.

**Why this module exists.** Two facts about the running server are needed in
several places that must never disagree with each other:

1. *Is dry run active?* -- consumed by the graph generator, the compile
   pipeline (stub fill), the LangGraph conversion and the run subprocess.
2. *Which credential has the user configured in the app, and under which
   environment variable does the SDK look for it?*

Answering (1) in four places by reading two different sources is how a
"dry run" compile ends up making one real API call, so the whole precedence
lives in :func:`dry_run_active`. Answering (2) by hand at each call site is how
a key ends up published under a name the provider's SDK does not read, so the
mapping lives in :mod:`swarm_builder.providers` and is applied here.

**Why the key is published into the process environment.** PydanticAI resolves
a known-name string (``openai:gpt-5``) by reading ``OPENAI_API_KEY`` from the
process environment, and the run subprocess
(``compile/run.py``) deliberately passes the server's environment through
unstripped so it can authenticate. Publishing the configured key once -- into
``os.environ``, under the provider's own variable name -- is therefore the one
mechanism that makes the in-process agent, the credential check and every child
process agree, with no new plumbing and no second place that knows how to build
a model. It happens on exactly two occasions: server startup, and a successful
settings save.

**Never cached.** ``workspace/settings.json`` is user-editable while the server
runs, so every function here re-reads it (via
:func:`swarm_builder.appconfig.load_config`) unless the caller already has an
:class:`~swarm_builder.appconfig.AppConfig` in hand.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from swarm_builder.appconfig import AppConfig, AppModelConfig, load_config
from swarm_builder.providers import PROVIDERS_BY_KEY, ProviderSpec

#: The environment variables that force dry run on. Each is read exactly as
#: the pre-existing code read it (``== "1"``), so a user who exported one of
#: them gets identical behaviour before and after this feature.
DRY_RUN_ENV_VARS: tuple[str, ...] = (
    "SWARM_FAKE_FILL",
    "SWARM_FAKE_GENERATE",
    "SWARM_RUN_TEST_MODEL",
)

#: The variable the generated project's tracer reads to swap in a keyless
#: ``TestModel`` (``compile/scaffold.py``'s ``STREAM_RUN_TEST_MODEL_ENV_VAR``).
#: Duplicated as a literal rather than imported: ``compile`` is a heavy
#: package and this module is imported by route handlers on every request.
RUN_TEST_MODEL_ENV_VAR = "SWARM_RUN_TEST_MODEL"

#: The value every one of :data:`DRY_RUN_ENV_VARS` must hold to count.
DRY_RUN_ENABLED_VALUE = "1"

#: The credential variables *this process* has published, so they can be
#: retracted when the configuration that justified them goes away. Only names
#: this module set are ever removed: a variable the user exported, or one a
#: container injected, is not ours to unset.
_PUBLISHED_BY_US: set[str] = set()

#: Serializes every mutation of a credential variable. Two request handlers
#: can touch the same names concurrently -- ``PUT /api/settings`` is a sync
#: handler (threadpool) while ``POST /api/settings/test`` is async and holds
#: its override across an ``await`` -- and without this, one handler's restore
#: can install another's rejected key for the rest of the process lifetime.
_SECRET_LOCK = threading.RLock()


def dry_run_forced_env_vars() -> list[str]:
    """Which dry-run variables are currently set to ``1``, in declaration order."""
    return [name for name in DRY_RUN_ENV_VARS if os.environ.get(name) == DRY_RUN_ENABLED_VALUE]


def dry_run_forced_by_env() -> bool:
    """Whether the environment alone forces dry run on."""
    return bool(dry_run_forced_env_vars())


def dry_run_active(cfg: AppConfig | None = None) -> bool:
    """Whether the whole pipeline runs against stubs and keyless test models.

    True when the app's own switch is on **or** any of
    :data:`DRY_RUN_ENV_VARS` is set to ``1``. The environment deliberately
    wins in one direction only -- it can force dry run on, but a UI switch
    can never turn a user's exported ``SWARM_FAKE_FILL=1`` back off -- because
    an offline or CI run that silently spent real credentials would be a far
    worse surprise than a locked switch.

    Args:
        cfg: A configuration already loaded. Omitted, the file is re-read.
    """
    if cfg is None:
        cfg = load_config()
    if cfg is not None and cfg.dry_run:
        return True
    return dry_run_forced_by_env()


def current_model_config(cfg: AppConfig | None = None) -> AppModelConfig | None:
    """The model configured in the app's own settings file, if any."""
    if cfg is None:
        cfg = load_config()
    if cfg is None or cfg.error is not None:
        # A file that cannot be parsed contributes no route: resolution falls
        # through to the inherited/env sources and the health check reports
        # the broken file in its own right, so a user is never left with a
        # silently ignored configuration.
        return None
    return cfg.model


def spec_for_config(model: AppModelConfig | None) -> ProviderSpec | None:
    """The provider spec behind ``model``, or ``None`` when unknown."""
    if model is None:
        return None
    return PROVIDERS_BY_KEY.get(model.provider)


def secret_env_overrides(cfg: AppConfig | None = None) -> dict[str, str]:
    """The environment variables that carry the configured credential.

    Empty when nothing is configured, when the provider takes no key
    (bedrock, whose AWS credentials come from the ambient environment), or
    when the configuration carries no key yet. Never includes the key under a
    name the provider's SDK does not read.
    """
    model = current_model_config(cfg)
    spec = spec_for_config(model)
    if model is None or spec is None or not model.api_key:
        return {}
    if spec.api_key_env is None:
        return {}
    return {spec.api_key_env: model.api_key}


def publish_secrets(path: Path | None = None) -> list[str]:
    """Reconcile this process's environment with the configured credential.

    Called at server startup and after a successful settings save. Returns the
    names that were set (for a name-only log line -- the value is never
    returned, printed or stored anywhere else).

    Reconciliation, not accumulation: a variable this module published earlier
    is **removed** when the new configuration no longer implies it, so
    clearing a key, changing provider, or repairing a broken settings file
    actually stops the app (and every subprocess it spawns, which inherits this
    environment) from authenticating with the old credential. A name this
    process never set is left alone: an ``export``ed key or a container secret
    is not ours to unset.
    """
    overrides = secret_env_overrides(load_config(path))
    with _SECRET_LOCK:
        for name in sorted(_PUBLISHED_BY_US - set(overrides)):
            os.environ.pop(name, None)
        _PUBLISHED_BY_US.intersection_update(overrides)
        for name, value in overrides.items():
            os.environ[name] = value
            _PUBLISHED_BY_US.add(name)
    return sorted(overrides)


@contextmanager
def temporary_secret_env(overrides: Mapping[str, str]) -> Iterator[None]:
    """Apply ``overrides`` for the duration of one call, then restore.

    Used by the Test-connection endpoint when a user tests a key that has not
    been saved yet: PydanticAI reads credentials from the environment, so a
    not-yet-persisted key has to be published somewhere for the call to use
    it. Every variable touched is restored to its previous value (or removed
    when it was absent) in a ``finally``, so a **rejected key never stays live**
    -- including when two requests overlap, because the whole window is held
    under the publication lock rather than only the assignment.
    """
    with _SECRET_LOCK:
        previous: dict[str, str | None] = {name: os.environ.get(name) for name in overrides}
        os.environ.update(overrides)
        try:
            yield
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def child_env_overrides(
    cfg: AppConfig | None = None, *, dry_run: bool | None = None
) -> dict[str, str]:
    """Extra environment for a subprocess this app launches for a graph's run.

    In dry run the generated tracer is told to use a keyless ``TestModel``
    (``SWARM_RUN_TEST_MODEL=1``), which is the same mechanism
    ``compile/scaffold.py`` already emits into every project. When dry run is
    off the overlay is empty and the child inherits the server's environment
    unchanged -- the behaviour a real run has today.

    ``dry_run`` lets a caller that already froze the decision at job start pass
    it in, so the child is configured from the same value the rest of the job
    used rather than from a file that may have changed in between. Reading it
    here is the fallback for callers that have nothing to freeze yet.
    """
    active = dry_run_active(cfg) if dry_run is None else dry_run
    if not active:
        return {}
    return {RUN_TEST_MODEL_ENV_VAR: DRY_RUN_ENABLED_VALUE}


def child_process_env(
    *,
    uv_cache_dir: Path,
    extra_env: Mapping[str, str] | None = None,
    dry_run: bool = False,
) -> dict[str, str]:
    """The complete environment for a subprocess this app launches.

    One place, because the dry-run case is a *safety* property rather than a
    convenience flag: in dry run the child is told to use the keyless
    ``TestModel`` **and** the provider credentials are removed from its
    environment. Relying on the generated tracer to honour the flag alone would
    make "a dry run cannot spend money" a promise about code the user might
    have edited, or a project generated before the flag existed; removing the
    credentials makes it a property of the environment instead. Harmless for
    the real path, where the child needs them.

    The credential names are taken from the compile pipeline's own strip list,
    so the variables removed here and the variables the keyless validation gate
    removes can never drift apart.
    """
    from swarm_builder.compile.validate import CREDENTIAL_ENV_VARS_TO_STRIP

    env: dict[str, str] = {
        **os.environ,
        "UV_CACHE_DIR": str(uv_cache_dir),
        "PYTHONUNBUFFERED": "1",
    }
    if dry_run:
        for name in CREDENTIAL_ENV_VARS_TO_STRIP:
            env.pop(name, None)
    env.update(extra_env or {})
    return env


def is_published_by_us(name: str) -> bool:
    """Whether this process published ``name`` (introspection/testing seam)."""
    return name in _PUBLISHED_BY_US


__all__ = [
    "DRY_RUN_ENABLED_VALUE",
    "child_process_env",
    "is_published_by_us",
    "DRY_RUN_ENV_VARS",
    "RUN_TEST_MODEL_ENV_VAR",
    "child_env_overrides",
    "current_model_config",
    "dry_run_active",
    "dry_run_forced_by_env",
    "dry_run_forced_env_vars",
    "publish_secrets",
    "secret_env_overrides",
    "spec_for_config",
    "temporary_secret_env",
]
