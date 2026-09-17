"""Hermetic OpenAPI schema regression guard -- no network, no ``npx``.

Confirms ``app.openapi()`` builds successfully and that the
``SwarmGraph`` component schema is genuinely camelCase, which is the
"one schema, two consumers" invariant Group 6's TypeScript generation
depends on.
"""

from __future__ import annotations

from swarm_builder.main import create_app


def test_openapi_schema_builds_and_swarm_graph_is_camel_case() -> None:
    app = create_app()
    schema = app.openapi()

    assert isinstance(schema, dict)
    assert "components" in schema
    assert "schemas" in schema["components"]
    assert "SwarmGraph" in schema["components"]["schemas"]

    properties = schema["components"]["schemas"]["SwarmGraph"]["properties"]

    assert "entryNodeId" in properties
    assert "stateFields" in properties
    assert "updatedAt" in properties

    assert "entry_node_id" not in properties
    assert "state_fields" not in properties
    assert "updated_at" not in properties
