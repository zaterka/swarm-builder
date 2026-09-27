"""Tests for ``GET /api/database-starters`` (PLAN-DB-NODES.md §4.7).

Two things this route owes the canvas, and one seam it has to keep:

- **The payload a node is created from.** A new database node copies ``spec`` *and*
  ``io`` into the graph document, so the document is self-contained and the Inspector
  shows what will actually run -- nothing is substituted at scaffold time. That makes
  the served entry the node's initial state, and a missing or renamed field would be a
  node that cannot be compiled.
- **``envVars`` is exactly one engine's variables.** ``SWARM_DB_MODE`` is app-wide, not
  an engine's, so it belongs to no entry: a canvas that read the mode switch off a
  starter would offer it three times, as if each engine had its own.
- **The lazy-import degradation seam.** The catalog is read (three ``starter.json``
  files are validated) inside the handler, so a broken catalog fails *this* route with a
  503 while every other endpoint keeps working. Both halves of "cannot be read" are
  covered: an import failure, and a `starter.json` that is missing at read time -- the
  second is why the handler catches more than ``ImportError``, and catching only
  ``ImportError`` would answer 500 and leave the palette with no disabled state.
"""

from __future__ import annotations

import builtins
import json
import pathlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swarm_builder.main import create_app
from swarm_builder.templates.database import DATABASE_CATALOG, KIND_ORDER

#: The variable that switches every engine at once. Named here rather than imported from
#: the generated project: the claim under test is that the *route* never serves it as an
#: engine's variable, and a test that read the name from the code it audits could not
#: notice a rename.
_DB_MODE_VARIABLE = "SWARM_DB_MODE"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The app under test, with its state pointed at a temporary root."""
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh_home"))
    return TestClient(create_app())


def _starters(client: TestClient) -> dict[str, dict[str, object]]:
    """The route's payload, indexed by kind."""
    response = client.get("/api/database-starters")
    assert response.status_code == 200, response.text
    return {entry["kind"]: entry for entry in response.json()}


def test_the_route_serves_one_entry_per_database_kind(client: TestClient) -> None:
    """One entry per kind, in the catalog's own order.

    The palette renders one entry per element and addresses the starter map by kind, so a
    missing kind is a node a user cannot create and a duplicated one is a picker with two
    identical rows.
    """
    response = client.get("/api/database-starters")

    assert response.status_code == 200, response.text
    assert [entry["kind"] for entry in response.json()] == list(KIND_ORDER)


def test_each_entry_is_the_catalogs_picker_facing_projection(client: TestClient) -> None:
    """``spec``/``io``/``liveExtra`` are the catalog's own values, field for field.

    Compared against the catalog rather than against literals: a starter that is edited
    must reach the client through this route, and a second hand-written copy of the default
    operation in the route would be a lookup that silently drifts from the file the
    generated project is rendered from.
    """
    starters = _starters(client)

    for kind, entry in DATABASE_CATALOG.items():
        served = starters[kind]
        assert served["spec"] == json.loads(entry.starter_spec.model_dump_json(by_alias=True)), (
            kind
        )
        assert served["io"] == {
            "inputType": entry.starter_io.input_type,
            "outputType": entry.starter_io.output_type,
        }, kind
        assert served["liveExtra"] == entry.live_extra_name, kind


def test_every_entry_serves_the_mandatory_str_to_rows_io_pair(client: TestClient) -> None:
    """A database node's ports are not a choice: ``str -> list[json]`` for all three kinds.

    This is the pair a node is created with, and the one Phase 1 enforces afterwards; a
    starter that served anything else would create a node the Inspector immediately has to
    report as invalid.
    """
    starters = _starters(client)

    assert {
        f"{entry['io']['inputType']}->{entry['io']['outputType']}"
        for entry in starters.values()
    } == {"str->list[json]"}


def test_env_vars_are_exactly_that_engines_variables_without_the_mode_switch(
    client: TestClient,
) -> None:
    """``envVars`` is one engine's variables -- and the app-wide mode switch is in none.

    ``SWARM_DB_MODE`` switches all three engines at once, so listing it under an engine
    would tell a user that engine has a mode of its own; and the generated README and
    ``.env.example`` are written from these same tuples, so an extra variable here is an
    extra variable in the exported project's documentation.
    """
    starters = _starters(client)

    for kind, entry in DATABASE_CATALOG.items():
        assert starters[kind]["envVars"] == list(entry.env_vars), kind
        assert _DB_MODE_VARIABLE not in starters[kind]["envVars"], kind
    # The three engines really do have distinct variables, so the loop above is not
    # comparing three empty lists.
    assert {variable for entry in starters.values() for variable in entry["envVars"]} == {
        "SWARM_SQL_DSN",
        "SWARM_NOSQL_DSN",
        "SWARM_NOSQL_COLLECTION",
        "SWARM_VECTOR_DSN",
        "SWARM_VECTOR_API_KEY",
        "SWARM_VECTOR_COLLECTION",
    }


def test_the_route_answers_503_when_the_catalog_cannot_be_imported(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken catalog must fail its own route, not the server.

    The catalog is imported inside the handler for exactly this reason: a user whose
    ``starter.json`` is unreadable still needs the rest of the app -- including the
    endpoints that let them diagnose it -- and the palette needs a defined state to render
    its disabled database entries from.
    """
    real_import = builtins.__import__

    def _blocked(name: str, *args: object, **kwargs: object):
        if name == "swarm_builder.templates.database":
            raise ImportError("the database catalog is unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)

    response = client.get("/api/database-starters")

    assert response.status_code == 503
    assert "database starters are not available" in response.json()["detail"]
    # The same process, the same moment: the agent catalog is untouched.
    assert client.get("/api/templates").status_code == 200


def test_a_missing_starter_file_is_also_a_503_and_not_a_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The catalog's *file-level* failure modes degrade to the same documented 503.

    The catalog validates three ``starter.json`` files while it is being read, so its
    failure modes include ``OSError`` and ``ValueError`` as well as ``ImportError``.
    Catching only the latter would answer 500 here -- a state the palette has no rendering
    for -- so the file is made unreadable and the route is asked what it says.
    """
    real_read_text = pathlib.Path.read_text

    def _read_text(self: pathlib.Path, *args: object, **kwargs: object) -> str:
        if self.name == "starter.json" and "templates/database" in self.as_posix():
            raise FileNotFoundError(f"no such file: {self}")
        return real_read_text(self, *args, **kwargs)

    # Drop the cached module so the handler's import really re-reads the catalog.
    monkeypatch.delitem(sys.modules, "swarm_builder.templates.database", raising=False)
    monkeypatch.setattr(pathlib.Path, "read_text", _read_text)

    response = client.get("/api/database-starters")

    assert response.status_code == 503
    assert "starter.json" in response.json()["detail"]
    assert client.get("/api/templates").status_code == 200
