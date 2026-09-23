"""``config.load_env_file`` and the generate route's credential refusals."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.exceptions import ModelHTTPError, UserError

from swarm_builder.compile import generate as generate_module
from swarm_builder.config import load_env_file
from swarm_builder.main import create_app


def test_load_env_file_sets_missing_vars_and_keeps_shell_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "\n"
        "SWARM_TEST_A=one\n"
        "export SWARM_TEST_B='two words'\n"
        'SWARM_TEST_C="quoted"\n'
        "SWARM_TEST_EMPTY=\n"
        "SWARM_TEST_SHELL=from-file\n"
        "not a pair\n"
    )
    for name in ("SWARM_TEST_A", "SWARM_TEST_B", "SWARM_TEST_C", "SWARM_TEST_EMPTY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SWARM_TEST_SHELL", "from-shell")

    loaded = load_env_file(env)

    assert loaded == ["SWARM_TEST_A", "SWARM_TEST_B", "SWARM_TEST_C"]
    import os

    assert os.environ["SWARM_TEST_A"] == "one"
    assert os.environ["SWARM_TEST_B"] == "two words"
    assert os.environ["SWARM_TEST_C"] == "quoted"
    assert "SWARM_TEST_EMPTY" not in os.environ
    assert os.environ["SWARM_TEST_SHELL"] == "from-shell"
    for name in ("SWARM_TEST_A", "SWARM_TEST_B", "SWARM_TEST_C"):
        monkeypatch.delenv(name)


def test_load_env_file_missing_is_a_noop(tmp_path: Path) -> None:
    assert load_env_file(tmp_path / "absent.env") == []


@pytest.fixture
def deepseek_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARM_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh_home"))
    monkeypatch.setenv("SWARM_MODEL", "deepseek:deepseek-v4-flash")
    monkeypatch.delenv("SWARM_BASE_URL", raising=False)
    monkeypatch.delenv("SWARM_API_KEY_ENV", raising=False)
    monkeypatch.delenv("SWARM_FAKE_GENERATE", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


def test_generate_refuses_up_front_when_the_provider_key_is_missing(deepseek_route: None) -> None:
    _ = deepseek_route
    response = TestClient(create_app()).post("/api/graphs/generate", json={"description": "x"})
    assert response.status_code == 503
    detail = response.json()["detail"]
    # App-native: the fix is named where the user can perform it, not as a
    # shell export or an editor instruction. The message is shared with the
    # health check, so it says exactly what the user must do and nothing about
    # where this particular request happened to fail.
    assert "DEEPSEEK_API_KEY" not in detail
    assert "Model settings" in detail
    assert "Dry run mode" in detail
    assert ".env" not in detail


def test_generate_maps_pydantic_ai_user_error_to_503(
    deepseek_route: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = deepseek_route
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    class _Boom:
        async def run(self, *args: object, **kwargs: object) -> None:
            raise UserError("Set the `DEEPSEEK_API_KEY` environment variable")

    monkeypatch.setattr(generate_module, "build_generate_agent", lambda model: _Boom())
    response = TestClient(create_app()).post("/api/graphs/generate", json={"description": "x"})
    assert response.status_code == 503
    assert "model configuration error" in response.json()["detail"]


def test_generate_maps_model_http_error_to_502(
    deepseek_route: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = deepseek_route
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    class _Refused:
        async def run(self, *args: object, **kwargs: object) -> None:
            raise ModelHTTPError(
                status_code=400,
                model_name="deepseek-flash",
                body={"message": "Thinking mode does not support this tool_choice"},
            )

    monkeypatch.setattr(generate_module, "build_generate_agent", lambda model: _Refused())
    response = TestClient(create_app()).post("/api/graphs/generate", json={"description": "x"})
    assert response.status_code == 502
    assert "tool_choice" in response.json()["detail"]


def test_generate_agent_uses_prompted_output_not_a_forced_tool() -> None:
    from pydantic_ai import PromptedOutput

    agent = generate_module.build_generate_agent("test")
    assert isinstance(agent.output_type, PromptedOutput)
