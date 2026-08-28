from __future__ import annotations

import json
from pathlib import Path

import pytest

from gameforge.adapters.llm import ModelResponse, ModelUsage
from gameforge.harness.project_query import answer_project_question


class QueryModel:
    def __init__(self, decision: dict[str, object]) -> None:
        self.decision = decision

    def complete(self, *, system: str, prompt: str) -> ModelResponse:
        assert "read-only" in system
        assert "approved_read_only_files" in prompt
        return ModelResponse(
            text=json.dumps(self.decision),
            usage=ModelUsage(20, 10, 0.01, cached_input_tokens=5),
            provider="scripted-query",
            model="fixture",
            latency_seconds=0.25,
        )


def test_project_query_returns_verified_line_evidence_without_mutation(tmp_path: Path) -> None:
    project = tmp_path / "project"
    script = project / "Enemy.cs"
    project.mkdir()
    script.write_text(
        "class Enemy\n{\n    private float speed = 3f;\n}\n",
        encoding="utf-8",
    )
    original = script.read_bytes()
    model = QueryModel(
        {
            "answer": "Enemy speed defaults to 3 units per second.",
            "citations": [{"path": "Enemy.cs", "line_start": 3, "line_end": 3}],
            "limitations": [],
        }
    )

    run = answer_project_question(
        root=tmp_path,
        project=project,
        question="Where is enemy speed controlled?",
        approved_paths=("Enemy.cs",),
        model=model,
    )

    assert run.result.status == "PASS"
    assert run.result.project_unchanged
    assert run.result.citations[0].excerpt == "    private float speed = 3f;"
    assert run.result.citations[0].sha256
    assert script.read_bytes() == original
    assert (run.directory / "result.json").is_file()
    assert (run.directory / "model-trace.json").is_file()
    assert (run.directory / "context-manifest.json").is_file()


def test_project_query_rejects_unverifiable_citation(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "Enemy.cs").write_text("one line\n", encoding="utf-8")
    model = QueryModel(
        {
            "answer": "Unsupported claim.",
            "citations": [{"path": "Enemy.cs", "line_start": 2, "line_end": 2}],
            "limitations": [],
        }
    )

    with pytest.raises(ValueError, match="line range exceeds"):
        answer_project_question(
            root=tmp_path,
            project=project,
            question="What is configured?",
            approved_paths=("Enemy.cs",),
            model=model,
        )


def test_project_query_splits_wide_source_range_into_bounded_excerpts(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "Damage.cs").write_text(
        "\n".join(f"line {number}" for number in range(1, 40)) + "\n",
        encoding="utf-8",
    )
    model = QueryModel(
        {
            "answer": "The damage flow spans the cited method.",
            "citations": [{"path": "Damage.cs", "line_start": 14, "line_end": 38}],
            "limitations": [],
        }
    )

    run = answer_project_question(
        root=tmp_path,
        project=project,
        question="How does damage flow?",
        approved_paths=("Damage.cs",),
        model=model,
    )

    assert [
        (citation.line_start, citation.line_end) for citation in run.result.citations
    ] == [(14, 34), (35, 38)]
    assert run.result.citations[0].excerpt.startswith("line 14\n")
    assert run.result.citations[1].excerpt == "line 35\nline 36\nline 37\nline 38"
