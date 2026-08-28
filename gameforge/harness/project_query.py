from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import Field, model_validator

from gameforge.adapters.llm import LanguageModel
from gameforge.harness.base import StrictModel
from gameforge.harness.preservation import snapshot_project
from gameforge.harness.project_context import ProjectContext, build_project_context

MAX_CITATION_SOURCE_LINES = 128
MAX_VERIFIED_EXCERPT_LINES = 21


class QueryCitation(StrictModel):
    path: str = Field(min_length=1)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)

    @model_validator(mode="after")
    def ordered_bounded_range(self) -> QueryCitation:
        if self.line_end < self.line_start:
            raise ValueError("citation line_end must be at or after line_start")
        if self.line_end - self.line_start + 1 > MAX_CITATION_SOURCE_LINES:
            raise ValueError(
                f"citation cannot span more than {MAX_CITATION_SOURCE_LINES} source lines"
            )
        return self


class QueryDecision(StrictModel):
    answer: str = Field(min_length=1, max_length=16_000)
    citations: tuple[QueryCitation, ...] = Field(min_length=1, max_length=16)
    limitations: tuple[str, ...] = Field(default=(), max_length=16)


class VerifiedQueryCitation(QueryCitation):
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    excerpt: str


class ProjectQueryResult(StrictModel):
    schema_version: int = 1
    query_id: str = Field(min_length=1)
    status: str = Field(pattern=r"^PASS$")
    question: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    citations: tuple[VerifiedQueryCitation, ...] = Field(min_length=1)
    limitations: tuple[str, ...] = ()
    approved_paths: tuple[str, ...] = Field(min_length=1)
    project_unchanged: bool
    provider: str
    model: str
    input_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
    latency_seconds: float = Field(ge=0)


@dataclass(frozen=True)
class ProjectQueryRun:
    directory: Path
    result: ProjectQueryResult


def answer_project_question(
    *,
    root: Path,
    project: Path,
    question: str,
    approved_paths: tuple[str, ...],
    model: LanguageModel,
) -> ProjectQueryRun:
    normalized_question = question.strip()
    if not normalized_question:
        raise ValueError("project question must not be empty")
    if len(normalized_question.encode("utf-8")) > 8 * 1024:
        raise ValueError("project question exceeds 8 KiB")
    context = build_project_context(project, approved_paths)
    if len(context.files) != len(approved_paths):
        available = {document.path for document in context.files}
        missing = sorted(set(approved_paths) - available)
        raise ValueError(f"every approved query path must exist: {missing}")

    before = snapshot_project(project)
    system = (
        "You are the read-only question-answering mode of a game-engine Harness. Answer only "
        "from the approved UTF-8 project files supplied in the payload. Do not propose or claim "
        "a file change. Every material claim must be grounded by one or more exact path and "
        "1-based inclusive line ranges. State uncertainty in limitations. Return exactly one "
        "JSON object with answer, citations, and limitations matching decision_schema."
    )
    prompt = json.dumps(
        {
            "question": normalized_question,
            "approved_read_only_files": [
                {
                    "path": document.path,
                    "sha256": document.sha256,
                    "content": document.content,
                }
                for document in context.files
            ],
            "decision_schema": {
                "answer": "non-empty answer string",
                "citations": [
                    {
                        "path": "exact approved path",
                        "line_start": "1-based integer",
                        "line_end": (
                            "1-based inclusive integer; cite the smallest useful range, "
                            f"up to {MAX_CITATION_SOURCE_LINES} source lines"
                        ),
                    }
                ],
                "limitations": ["zero or more concise uncertainty strings"],
            },
        },
        sort_keys=True,
    )
    response = model.complete(system=system, prompt=prompt)
    decision = QueryDecision.model_validate_json(response.text)
    citations = _verify_citations(decision, context)
    after = snapshot_project(project)
    unchanged = before.files == after.files
    if not unchanged:
        raise RuntimeError("read-only project query changed the project")

    query_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ-project-query")
    directory = (root / "runs" / "project-queries" / query_id).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    result = ProjectQueryResult(
        query_id=query_id,
        status="PASS",
        question=normalized_question,
        answer=decision.answer,
        citations=citations,
        limitations=decision.limitations,
        approved_paths=approved_paths,
        project_unchanged=unchanged,
        provider=response.provider,
        model=response.model,
        input_tokens=response.usage.input_tokens,
        cached_input_tokens=response.usage.cached_input_tokens,
        output_tokens=response.usage.output_tokens,
        reasoning_output_tokens=response.usage.reasoning_output_tokens,
        cost_usd=response.usage.cost_usd,
        latency_seconds=response.latency_seconds,
    )
    (directory / "result.json").write_text(
        result.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    (directory / "model-trace.json").write_text(
        json.dumps(
            response.trace_record(system=system, prompt=prompt),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (directory / "context-manifest.json").write_text(
        json.dumps(
            {
                "project": str(context.project_path),
                "files": [
                    {
                        "path": document.path,
                        "sha256": document.sha256,
                        "bytes": document.bytes,
                    }
                    for document in context.files
                ],
                "total_bytes": context.total_bytes,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return ProjectQueryRun(directory=directory, result=result)


def _verify_citations(
    decision: QueryDecision,
    context: ProjectContext,
) -> tuple[VerifiedQueryCitation, ...]:
    documents = {document.path: document for document in context.files}
    verified: list[VerifiedQueryCitation] = []
    for citation in decision.citations:
        try:
            document = documents[citation.path]
        except KeyError as error:
            raise ValueError(f"citation path is not approved: {citation.path}") from error
        lines = document.content.splitlines()
        if citation.line_end > len(lines):
            raise ValueError(
                f"citation line range exceeds {citation.path}: {citation.line_end}>{len(lines)}"
            )
        for line_start in range(
            citation.line_start,
            citation.line_end + 1,
            MAX_VERIFIED_EXCERPT_LINES,
        ):
            line_end = min(
                citation.line_end,
                line_start + MAX_VERIFIED_EXCERPT_LINES - 1,
            )
            verified.append(
                VerifiedQueryCitation(
                    path=citation.path,
                    line_start=line_start,
                    line_end=line_end,
                    sha256=document.sha256,
                    excerpt="\n".join(lines[line_start - 1 : line_end]),
                )
            )
    return tuple(verified)
