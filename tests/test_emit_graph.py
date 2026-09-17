"""Unit tests for emit_graph.py against the codegen contract rules
(PLAN.md "Codegen contract" 1-12) that are checkable from generated
source text alone, without a real uv sync (the full end-to-end proof
lives in test_codegen_fixtures.py)."""

from __future__ import annotations

from fixtures.graphs import (
    decision_branching_graph,
    fanout_join_graph,
    linear_chat_graph,
    orchestrator_graph,
)
from swarm_builder.compile.emit_graph import emit_graph


def test_never_calls_graph_constructor_directly() -> None:
    """Contract rule 4: Graph(...) is never called directly (fact 6)."""
    source = emit_graph(linear_chat_graph())
    assert "Graph(" not in source
    assert "GraphBuilder(" in source
    assert "builder.build()" in source


def test_uses_plain_call_step_form_with_explicit_node_id() -> None:
    """Contract rule 6: every builder.step carries an explicit node_id=."""
    source = emit_graph(linear_chat_graph())
    assert 'builder.step(intake, node_id="intake")' in source
    assert 'builder.step(chat_step, node_id="chat_step")' in source
    assert 'builder.step(summarize, node_id="summarize")' in source


def test_future_annotations_is_the_first_import() -> None:
    """Contract rule 12: every generated Python file starts with
    from __future__ import annotations."""
    source = emit_graph(linear_chat_graph())
    lines = [line for line in source.splitlines() if line.strip()]
    import_lines = [line for line in lines if line.startswith(("from ", "import "))]
    assert import_lines[0] == "from __future__ import annotations"


def test_join_emits_explicit_builder_join_never_two_bare_add_edges() -> None:
    """Contract rule 7: every fan-in goes through an explicit
    builder.join(...); two bare add_edge calls are never used as a
    fan-out (fact 13)."""
    source = emit_graph(fanout_join_graph())
    assert "builder.join(reduce_list_append, initial_factory=list" in source
    assert "builder.add_edge(left_node, join)" in source
    assert "builder.add_edge(right_node, join)" in source


def test_decision_branch_chain_precedes_add_edge_targeting_it() -> None:
    """Contract rule 8: a decision's complete .branch(...) chain is
    emitted BEFORE the add_edge that targets it (fact 14)."""
    source = emit_graph(decision_branching_graph())
    branch_chain_end = source.rindex(".branch(")
    add_edge_to_decision = source.index("builder.add_edge(classify_node, decision)")
    assert add_edge_to_decision > branch_chain_end, (
        "add_edge targeting the decision must come after the full .branch() chain"
    )
    # And the decision variable is rebound each time, not a fresh name.
    assert source.count("decision = decision.branch(") == 2
    assert source.count("decision = builder.decision(") == 1


def test_literal_import_present_only_when_a_decision_node_exists() -> None:
    """B3: from typing import Literal is needed for builder.match(Literal[...])
    -- an expression, not deferred by __future__ import annotations."""
    with_decision = emit_graph(decision_branching_graph())
    assert "from typing import Literal" in with_decision

    without_decision = emit_graph(linear_chat_graph())
    assert "from typing import Literal" not in without_decision


def test_port_type_never_interpolated_verbatim() -> None:
    """Contract rule 9: PortType goes through the annotation table, never
    interpolated verbatim (fact 17) -- output_type=json would be a
    NameError; output_type=list[str] is the real annotation."""
    source = emit_graph(fanout_join_graph())
    assert "output_type=list[str]" in source
    assert "output_type=json" not in source


def test_delegate_edge_emits_no_add_edge_and_no_step_for_child() -> None:
    """I1: delegate edges emit no add_edge at all -- otherwise the child
    runs twice."""
    source = emit_graph(orchestrator_graph())
    assert "child_agent" not in source  # no step import, no add_edge reference
    assert "add_edge" in source  # but the parent still gets its own edges


def test_start_and_end_edges_are_always_emitted() -> None:
    source = emit_graph(linear_chat_graph())
    assert "builder.add_edge(builder.start_node, intake_node)" in source
    assert "builder.add_edge(summarize_node, builder.end_node)" in source


def test_multiple_sinks_each_get_an_end_edge() -> None:
    """B1: exit_node_id is not the only sink -- spike/branching wires
    BOTH big and small to end_node."""
    source = emit_graph(decision_branching_graph())
    assert "builder.add_edge(big_node, builder.end_node)" in source
    assert "builder.add_edge(small_node, builder.end_node)" in source


def test_broadcast_fork_ids_predicted_for_fanout_source() -> None:
    """fact 32: the synthetic fork id is predictable and computed, not
    discovered."""
    source = emit_graph(fanout_join_graph())
    assert 'BROADCAST_FORK_NODE_IDS: dict[str, str] = {"split": "split_broadcast_fork"}' in source


def test_no_broadcast_fork_constant_when_no_fanout() -> None:
    source = emit_graph(linear_chat_graph())
    assert "BROADCAST_FORK_NODE_IDS" not in source


def test_graph_run_return_value_used_directly_in_dry_run_template() -> None:
    """Contract rule 10: graph.run()'s return value is used directly --
    no .output access, no End assertion. Checked on the emitted
    dry_run.py, not graph.py, since that's where graph.run() is called."""
    import tempfile
    from pathlib import Path

    from swarm_builder.compile import default_scaffold_model
    from swarm_builder.compile.scaffold import scaffold

    with tempfile.TemporaryDirectory() as tmp:
        scaffold(linear_chat_graph(), Path(tmp), default_scaffold_model())
        dry_run_text = (Path(tmp) / "validate" / "dry_run.py").read_text()
    assert "result.output" not in dry_run_text
    assert "out.output" not in dry_run_text
    assert "isinstance(out, End)" not in dry_run_text
    assert "from pydantic_graph import End" not in dry_run_text
    assert "await graph.run(" in dry_run_text


def test_every_emitted_py_file_starts_with_future_annotations() -> None:
    """Contract rule 12, whole-scaffold invariant (B16): every generated
    Python file starts with from __future__ import annotations, not just
    graph.py."""
    import tempfile
    from pathlib import Path

    from swarm_builder.compile import default_scaffold_model
    from swarm_builder.compile.scaffold import scaffold

    with tempfile.TemporaryDirectory() as tmp:
        result = scaffold(orchestrator_graph(), Path(tmp), default_scaffold_model())
        py_files = [p for p in result.written_paths if p.suffix == ".py"]
        assert py_files, "expected at least one emitted .py file"
        for py_file in py_files:
            text = py_file.read_text()
            lines = [line for line in text.splitlines() if line.strip()]
            import_lines = [line for line in lines if line.startswith(("from ", "import "))]
            assert import_lines and import_lines[0] == "from __future__ import annotations", (
                f"{py_file} does not start its imports with "
                "'from __future__ import annotations'"
            )


def test_scaffolding_the_same_fixture_twice_is_byte_identical() -> None:
    """Determinism check (nice-to-have from planReview): scaffolding the
    same fixture twice must produce byte-identical output for every
    emitted file -- this is also the cheapest guard against the kind of
    process-global corruption B5 found."""
    import tempfile
    from pathlib import Path

    from swarm_builder.compile import default_scaffold_model
    from swarm_builder.compile.scaffold import scaffold

    with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
        result_a = scaffold(fanout_join_graph(), Path(tmp_a), default_scaffold_model())
        result_b = scaffold(fanout_join_graph(), Path(tmp_b), default_scaffold_model())

        rel_a = sorted(p.relative_to(tmp_a) for p in result_a.written_paths)
        rel_b = sorted(p.relative_to(tmp_b) for p in result_b.written_paths)
        assert rel_a == rel_b

        for rel_path in rel_a:
            text_a = (Path(tmp_a) / rel_path).read_text()
            text_b = (Path(tmp_b) / rel_path).read_text()
            assert text_a == text_b, f"{rel_path} differs between two scaffolds of the same fixture"
