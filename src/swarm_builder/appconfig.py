"""The application's own model settings file: ``<workspace>/settings.json``.

**Why this module exists.** Until now the only ways to tell Swarm Builder
which model to spend were an inherited harness ``settings.yaml`` and the
``SWARM_MODEL`` environment variable -- both of which require knowing about a
file or a variable that lives *outside* this application. A new user should be
able to pick a provider and paste a key in the UI, so this module owns a small
JSON document that the application itself reads and writes.

**Why JSON, and why under the workspace.** The file sits next to the graphs and
generated projects it configures (``<workspace>/settings.json``, overridable
with ``SWARM_CONFIG``), is written with ``0600`` permissions because it holds a
credential, and lives inside the git-ignored ``workspace/`` directory so it can
never be committed by accident.

**What this module deliberately does not do.** It never mutates the process
environment -- publishing the key so PydanticAI, the run subprocess and the
generated project can read it is :mod:`swarm_builder.runtime`'s job. It also
never imports ``inherit``: the ``RouteConfig`` a resolved in-app model needs is
synthesized by ``inherit.settings`` itself, which keeps the import graph
acyclic (``inherit.settings`` → ``appconfig`` → ``providers`` →
``known_models``).

**Never cached, never raising.** The same rule that governs ``settings.yaml``
applies here: a user can flip the dry-run switch or replace a key between two
requests in the same running server process, so every public function re-reads
the file from disk. A missing file is a normal state (``None``); a file that
exists but cannot be read or parsed is reported as an ``error`` string on the
returned :class:`AppConfig`, never as an exception, so a broken file can never
crash a request that merely wanted to know the current model.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from swarm_builder import config
from swarm_builder.providers import PROVIDERS_BY_KEY, ProviderSpec

#: The file's basename inside the workspace (and the name ``SWARM_CONFIG``
#: overrides wholesale).
CONFIG_FILENAME = "settings.json"

#: Written into the file so a future reader can migrate it. Unknown *values*
#: are tolerated on read (see :func:`load_config`); this exists so a shape
#: change can be recognized rather than guessed at.
CONFIG_VERSION = 1


@dataclass(frozen=True)
class AppModelConfig:
    """The model route configured in the application's own settings file.

    ``api_key`` holds the credential itself -- unlike
    :class:`~swarm_builder.inherit.settings.RouteConfig`, which names only the
    environment variable a key should be read from. It is never rendered into
    a response body or a generated project: only
    :func:`swarm_builder.runtime.secret_env_overrides` and the settings
    response's four-character hint may touch it.
    """

    provider: str
    model: str
    base_url: str | None = None
    api_key: str | None = None
    reasoning_effort: str | None = None

    @property
    def label(self) -> str:
        """``provider/model`` for messages; never includes the key."""
        return f"{self.provider}/{self.model}"


@dataclass(frozen=True)
class AppConfig:
    """The result of one :func:`load_config` call.

    ``error`` distinguishes two very different "nothing useful here" states
    that must never be conflated -- exactly as
    :class:`~swarm_builder.inherit.settings.Settings` does for the inherited
    file:

    - the file does not exist at all (a normal state: nothing has been
      configured in the app yet) -- reported by :func:`load_config` returning
      ``None``, not by this dataclass;
    - the file exists but is unreadable or not valid JSON (a real user
      mistake, and one the settings screen must offer to fix rather than
      silently ignore) -- reported here with ``error`` set, ``model=None`` and
      ``dry_run=False``.
    """

    model: AppModelConfig | None = None
    dry_run: bool = False
    error: str | None = None

    @property
    def has_api_key(self) -> bool:
        """Whether a key is stored for the configured model."""
        return bool(self.model is not None and self.model.api_key)


def config_path(path: Path | None = None) -> Path:
    """Resolve where the app's settings file lives.

    An explicit ``path`` always wins (that is the test/caller seam);
    otherwise ``SWARM_CONFIG``; otherwise ``<workspace>/settings.json``,
    which is what :func:`swarm_builder.config.get_app_config_path` returns.
    """
    if path is not None:
        return path
    return config.get_app_config_path()


def _clean_str(value: object) -> str | None:
    """Coerce one parsed JSON scalar to a non-empty, stripped string or ``None``.

    Mirrors ``inherit/settings.py``'s ``_coerce_str``: a JSON number or
    boolean in a string field is treated as absent rather than stringified,
    so a hand-edited file cannot produce a model id of ``"True"``.

    A value containing a control character (a newline, a NUL, an escape) is
    also treated as absent. These strings are interpolated into a generated
    project's ``.env.example`` and ``deps.py`` as whole lines, so a model id
    such as ``"gpt-4o\nANTHROPIC_API_KEY=..."`` would otherwise smuggle a
    forged credential line into an artefact. Rejecting the value here means
    validation then reports the field as missing, which is the honest
    outcome: nothing usable was supplied.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        return None
    return text


def load_config(path: Path | None = None) -> AppConfig | None:
    """Read the application's model settings.

    Returns ``None`` when the file does not exist -- the normal "not
    configured yet" state. Returns an :class:`AppConfig` in every other case,
    with ``error`` set when the file exists but cannot be read or parsed.

    Tolerant by construction, for the same reason the inherited settings
    reader is: the document is written by this application but may be
    hand-edited between two requests, an unknown key must never break a
    request, and a section in an unexpected shape contributes nothing rather
    than raising.
    """
    target = config_path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        return AppConfig(error=f"failed to read {target}: {exc}")

    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        return AppConfig(error=f"failed to parse {target}: {exc}")

    if not isinstance(document, dict):
        return AppConfig(error=f"{target} must contain a JSON object")

    raw_model = document.get("model")
    model: AppModelConfig | None = None
    if isinstance(raw_model, dict):
        provider = _clean_str(raw_model.get("provider"))
        model_id = _clean_str(raw_model.get("model"))
        if provider is not None and model_id is not None:
            model = AppModelConfig(
                provider=provider,
                model=model_id,
                base_url=_clean_str(raw_model.get("baseUrl")),
                api_key=_clean_str(raw_model.get("apiKey")),
                reasoning_effort=_clean_str(raw_model.get("reasoningEffort")),
            )

    # A missing or non-boolean `dryRun` is "off": an ambiguous value must not
    # silently switch the whole pipeline into stub mode.
    dry_run = document.get("dryRun") is True

    return AppConfig(model=model, dry_run=dry_run, error=None)


def _has_userinfo(url: str) -> bool:
    """Report whether a URL embeds credentials (``https://user:key@host``)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return bool(parts.username or parts.password)


def strip_userinfo(url: str | None) -> str | None:
    """Return ``url`` with any embedded credentials removed.

    A base URL is rendered verbatim into a generated project's ``deps.py``
    and ``.env.example``, so a URL carrying ``user:password@`` would write a
    secret into an artifact. Saving rejects such a URL outright
    (:func:`validate_model_config`); this function is the belt-and-braces
    second line of defence for a value that reached disk anyway (a
    hand-edited file).
    """
    if url is None:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not (parts.username or parts.password):
        return url
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def _has_control_chars(text: str) -> bool:
    """Whether ``text`` contains a control character (newline, NUL, escape)."""
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in text)


def validate_model_config(model: AppModelConfig) -> list[str]:
    """Return the human-readable reasons ``model`` cannot be saved (``[]`` = valid).

    Deliberately a *list* rather than a raise: the settings screen shows every
    problem at once, so a user fixing a form does not have to resubmit it once
    per mistake.

    **A missing API key is not one of these reasons.** Saving a provider and a
    model with no key is a legitimate configuration -- the key may already be
    in this server's environment (an exported ``OPENAI_API_KEY``, a ``.env``
    file, a container secret), which is exactly the advanced workflow the app
    promises to keep working. Whether a usable credential exists right now is
    a *runtime* fact, reported by ``GET /api/health``'s ``run_ready`` and by
    the Test-connection button, not something to refuse at save time. What
    this function does reject is everything that would fail later, deeper and
    less clearly: an unknown provider, a blank model id, and a base URL that
    is missing, malformed, or carrying credentials into a generated project.
    """
    problems: list[str] = []

    spec = PROVIDERS_BY_KEY.get(model.provider)
    if spec is None:
        known = ", ".join(sorted(PROVIDERS_BY_KEY))
        return [f"unknown provider {model.provider!r} (expected one of: {known})"]

    if not model.model.strip():
        problems.append("a model id is required")
    elif _has_control_chars(model.model):
        # This value is spliced into a generated project's .env.example and
        # deps.py as a whole line, so a newline in it would forge a credential
        # line in an artefact the user ships.
        problems.append("the model id must not contain line breaks or control characters")

    if model.base_url is not None and _has_control_chars(model.base_url):
        problems.append("the base URL must not contain line breaks or control characters")

    if model.base_url is not None and not spec.requires_base_url:
        problems.append(
            f"a base URL is only supported for the {PROVIDERS_BY_KEY['custom'].label!r} "
            f"provider, not {spec.label!r}"
        )
    elif spec.requires_base_url:
        base_url = model.base_url or ""
        if not base_url:
            problems.append(f"a base URL is required for {spec.label!r}")
        elif not base_url.startswith(("http://", "https://")):
            problems.append("the base URL must start with http:// or https://")
        elif _has_userinfo(base_url):
            problems.append(
                "the base URL must not contain credentials; use the API key field instead"
            )

    return problems


def config_problems(cfg: AppConfig | None) -> list[str]:
    """Every reason this configuration is not usable, read errors included.

    Two sources of trouble that must both reach the user: the file could not be
    read at all (``cfg.error``), or it parsed but the entry it holds does not
    describe a usable route (an unknown provider, a blank model id, a
    malformed base URL). Resolution ignores either case in favour of the next
    source -- an entry that cannot build a model must not shadow a working
    configured one -- so this function is what keeps that quiet fallthrough
    from being *silent*.
    """
    if cfg is None:
        return []
    if cfg.error is not None:
        return [cfg.error]
    if cfg.model is None:
        return []
    return validate_model_config(cfg.model)


def spec_for(model: AppModelConfig) -> ProviderSpec | None:
    """The :class:`ProviderSpec` for ``model``'s provider, or ``None``."""
    return PROVIDERS_BY_KEY.get(model.provider)


def with_api_key(model: AppModelConfig, api_key: str | None) -> AppModelConfig:
    """Return ``model`` with its key replaced (``None`` clears it)."""
    cleaned = _clean_str(api_key)
    return replace(model, api_key=cleaned)


def _same_endpoint(existing: AppModelConfig, provider: str, base_url: str | None) -> bool:
    """Whether a submission still points at the same place as ``existing``.

    Both halves matter. Provider alone is not enough: the custom
    OpenAI-compatible provider is one key standing for *any* endpoint, so
    comparing only the provider would let a stored key follow a changed base
    URL to a different host -- the app would then authenticate to whoever the
    new URL names, and the Test button would send them the credential. A
    changed base URL therefore requires the key to be re-entered.
    """
    if existing.provider != provider:
        return False
    return (existing.base_url or None) == (base_url or None)


def apply_update(
    existing: AppModelConfig | None,
    provider: str,
    model_id: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    clear_api_key: bool = False,
    reasoning_effort: str | None = None,
) -> AppModelConfig:
    """Merge a settings-screen submission onto the stored configuration.

    The merge rule exists so a user editing only the model id does not have to
    retype their API key: an omitted or blank ``api_key`` *keeps* the stored
    key when the endpoint is unchanged (:func:`_same_endpoint`),
    ``clear_api_key=True`` removes it, and any non-blank ``api_key`` wins. A
    key is never carried across a change of provider *or* of base URL -- a
    DeepSeek key must not be published as ``OPENAI_API_KEY``, and a key for one
    self-hosted endpoint must not be sent to another -- so such a change with
    no new key yields a configuration whose missing credential the runtime
    checks then report.
    """
    cleaned_base_url = _clean_str(base_url)
    stored_key = None
    if (
        existing is not None
        and not clear_api_key
        and _same_endpoint(existing, provider, cleaned_base_url)
    ):
        stored_key = existing.api_key

    # A blank submission (whitespace, or an untouched password field posting
    # "") means "not supplied", never "delete my key".
    supplied_key = _clean_str(api_key)
    if clear_api_key:
        resolved_key = None
    elif supplied_key is not None:
        resolved_key = supplied_key
    else:
        resolved_key = stored_key

    return AppModelConfig(
        provider=provider,
        model=model_id.strip(),
        base_url=cleaned_base_url,
        api_key=resolved_key,
        reasoning_effort=_clean_str(reasoning_effort),
    )


def save_config(cfg: AppConfig, path: Path | None = None) -> None:
    """Write ``cfg`` to disk atomically, with owner-only permissions.

    The temporary file is created with ``0600`` *before* it receives anything
    (rather than chmod-ing after the rename), so the key is never briefly
    world-readable, and ``os.replace`` makes the swap atomic: a reader either
    sees the whole previous document or the whole new one. The parent directory
    is synced afterwards so the rename survives a crash.

    A symlink at ``target`` is replaced by the rename rather than written
    through: the app owns this file, and following a link set up by something
    else would let it write a credential wherever that link points.

    Raises:
        OSError: When the file cannot be written (an unwritable workspace, a
            directory in the way). The route layer translates this into an
            app-native HTTP error; this function does not swallow it, because
            a settings screen that silently failed to save a key would be
            worse than one that said so.
    """
    target = config_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    document: dict[str, object] = {"version": CONFIG_VERSION, "dryRun": cfg.dry_run}
    if cfg.model is not None:
        document["model"] = {
            "provider": cfg.model.provider,
            "model": cfg.model.model,
            "baseUrl": cfg.model.base_url,
            "apiKey": cfg.model.api_key,
            "reasoningEffort": cfg.model.reasoning_effort,
        }

    payload = json.dumps(document, indent=2, sort_keys=False) + "\n"

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # A rename is atomic for a *reader*, but the directory entry itself is
        # only durable once the parent has been synced -- otherwise a crash
        # could leave the previous document (or nothing) behind, and a
        # zero-length settings file would read as "no model configured".
        os.replace(tmp_path, target)
        dir_fd = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        # Leave no half-written document (nor a stray temp file) behind, and
        # never leak the descriptor when the write itself failed.
        try:
            os.close(fd)
        except OSError:
            pass
        tmp_path.unlink(missing_ok=True)
        raise


__all__ = [
    "CONFIG_FILENAME",
    "CONFIG_VERSION",
    "AppConfig",
    "AppModelConfig",
    "apply_update",
    "config_path",
    "config_problems",
    "load_config",
    "save_config",
    "spec_for",
    "strip_userinfo",
    "validate_model_config",
    "with_api_key",
]
