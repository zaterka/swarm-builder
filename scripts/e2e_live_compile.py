"""Live end-to-end compile against a real provider.

Runs one compile through the real HTTP server with real credentials, so the
fill agent's max-token budget and read_file recovery are exercised exactly as
a user would. Not part of pytest and not run in CI -- it spends real provider
credits and needs a key.

Usage (the key travels only via the environment, never on the command line):

    SWARM_E2E_PROVIDER=deepseek-official \
    SWARM_E2E_MODEL=deepseek-v4-flash \
    SWARM_E2E_API_KEY=sk-... \
    uv run python scripts/e2e_live_compile.py

Optional: SWARM_E2E_BASE_URL (custom endpoints), SWARM_E2E_PORT (default 18420),
SWARM_E2E_MAX_TOKENS (exercise the settings override).

The script uses a throwaway workspace (mktemp), writes the key into its
git-ignored settings.json via the settings API, and removes the directory on
exit -- nothing is written to the repository.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from swarm_builder.models import (  # noqa: E402
    BranchEdge,
    DecisionBranch,
    DecisionSpec,
    NodeIo,
    Position,
    ProgrammaticSpec,
    SeqEdge,
    StateField,
    SwarmGraph,
    SwarmNode,
)


def _pos(x: float, y: float) -> Position:
    return Position(x=x, y=y)


def _fixture_graph() -> SwarmGraph:
    classify = SwarmNode(
        id="classify",
        kind="programmatic",
        title="Classify",
        intent="Classify the input by length into big or small.",
        position=_pos(0, 0),
        io=NodeIo(input_type="str", output_type="str"),
        writes=["length_bucket"],
        programmatic=ProgrammaticSpec(needs=[], signature_hint="length > 3 -> big else small"),
    )
    decision = SwarmNode(
        id="decision",
        kind="decision",
        title="Decision",
        intent="Dispatch by length bucket.",
        position=_pos(1, 0),
        io=NodeIo(input_type="str", output_type="str"),
        decision=DecisionSpec(
            branches=[
                DecisionBranch(match="big", target_node_id="big"),
                DecisionBranch(match="small", target_node_id="small"),
            ],
            note="classify by length",
        ),
    )
    big = SwarmNode(
        id="big",
        kind="programmatic",
        title="Big",
        intent="Handle a big classification.",
        position=_pos(2, -1),
        io=NodeIo(input_type="str", output_type="str"),
        programmatic=ProgrammaticSpec(needs=[], signature_hint="prefix with BIG:"),
    )
    small = SwarmNode(
        id="small",
        kind="programmatic",
        title="Small",
        intent="Handle a small classification.",
        position=_pos(2, 1),
        io=NodeIo(input_type="str", output_type="str"),
        programmatic=ProgrammaticSpec(needs=[], signature_hint="prefix with small:"),
    )
    return SwarmGraph(
        id="e2e-decision-branching",
        name="E2E decision branching",
        entry_node_id="classify",
        exit_node_id="big",
        state_fields=[StateField(name="length_bucket", type="str", default='""')],
        nodes=[classify, decision, big, small],
        edges=[
            SeqEdge(kind="seq", id="e1", source="classify", target="decision"),
            BranchEdge(kind="branch", id="e2", source="decision", target="big", match="big"),
            BranchEdge(kind="branch", id="e3", source="decision", target="small", match="small"),
        ],
        updated_at=datetime.now(UTC),
    )


def _request(method: str, url: str, body: object | None = None) -> dict[str, object]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"content-type": "application/json"} if data is not None else {}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"{method} {url} -> {exc.code}: {detail}") from exc


def main() -> int:
    provider = os.environ["SWARM_E2E_PROVIDER"]
    model = os.environ["SWARM_E2E_MODEL"]
    api_key = os.environ["SWARM_E2E_API_KEY"]
    base_url = os.environ.get("SWARM_E2E_BASE_URL")
    port = int(os.environ.get("SWARM_E2E_PORT", "18420"))
    max_tokens = os.environ.get("SWARM_E2E_MAX_TOKENS")
    root = f"http://127.0.0.1:{port}"

    workspace = Path(tempfile.mkdtemp(prefix="swarm-e2e-"))
    dsh_home = workspace / "dsh_home"
    env = {
        **os.environ,
        "SWARM_WORKSPACE": str(workspace / "workspace"),
        "DSH_HOME": str(dsh_home),
        "PORT": str(port),
        "SWARM_HOST": "127.0.0.1",
        "UV_CACHE_DIR": str(REPO_ROOT / ".uv-cache"),
    }

    server = subprocess.Popen(
        [sys.executable, "-m", "swarm_builder.main"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        _wait_ready(root)
        model_input: dict[str, object] = {
            "provider": provider,
            "model": model,
            "baseUrl": base_url,
            "apiKey": api_key,
        }
        if max_tokens is not None:
            model_input["maxTokens"] = int(max_tokens)
        saved = _request("PUT", f"{root}/api/settings", {"model": model_input})
        print(f"settings saved: provider={provider} model={model} "
              f"maxTokens={saved['model'].get('maxTokens')}")

        graph = _fixture_graph()
        graph_id = graph.id
        put = _request(
            "PUT", f"{root}/api/graphs/{graph_id}", json.loads(graph.model_dump_json())
        )
        print(f"graph saved: {put['name']!r}")

        started = _request("POST", f"{root}/api/compile", {"graphId": graph_id})
        compile_id = started["compileId"]
        print(f"compile started: {compile_id}")

        status = _poll(root, compile_id)
        if status.get("status") != "succeeded":
            print(json.dumps(status, indent=2))
            return 1

        export = _request("GET", f"{root}/api/graphs/{graph_id}/export")
        project_path = export["projectPath"]
        print(f"compile succeeded; project at {project_path}")

        if not str(project_path).endswith("projects/e2e-decision-branching"):
            raise RuntimeError(
                f"expected a name-slug project path ending in "
                f"projects/e2e-decision-branching, got {project_path}"
            )
        print("name-slug project directory verified")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(workspace, ignore_errors=True)


def _wait_ready(root: str, *, attempts: int = 60) -> None:
    for _ in range(attempts):
        try:
            urllib.request.urlopen(f"{root}/api/health", timeout=2).read()
            return
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    raise RuntimeError("server did not become ready in time")


def _poll(root: str, compile_id: str, *, timeout_s: int = 600) -> dict[str, object]:
    deadline = time.time() + timeout_s
    last: dict[str, object] = {}
    while time.time() < deadline:
        last = _request("GET", f"{root}/api/compile/{compile_id}")
        if last.get("status") in {"succeeded", "failed", "cancelled"}:
            return last
        time.sleep(2)
    raise RuntimeError(f"compile did not reach a terminal state: {last}")


if __name__ == "__main__":
    raise SystemExit(main())
