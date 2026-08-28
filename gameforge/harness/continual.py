from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from pydantic import Field, model_validator

from gameforge.harness.base import StrictModel
from gameforge.harness.events import EventKind, EventStore
from gameforge.harness.game_tasks import EngineEnvironment
from gameforge.harness.skills import SkillCompatibility


class KnowledgeScope(StrEnum):
    RUN = "run"
    PROJECT = "project"
    GLOBAL = "global"


class KnowledgeType(StrEnum):
    PROMPT = "prompt"
    MEMORY = "memory"
    SKILL = "skill"
    WORKER_SPEC = "worker_spec"


class ProposalStatus(StrEnum):
    PROPOSED = "proposed"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


class KnowledgeEntry(StrictModel):
    id: str = Field(min_length=1)
    type: KnowledgeType
    scope: KnowledgeScope
    content: str = Field(min_length=1, max_length=100_000)
    source_run_id: str = Field(min_length=1)
    trigger_event_ids: tuple[str, ...] = Field(min_length=1)
    project_id: str | None = None
    project_revision_ids: tuple[str, ...] = ()
    environment_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    compatibility: SkillCompatibility | None = None
    executable_manifest_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def scope_and_execution_are_safe(self) -> KnowledgeEntry:
        if self.scope is KnowledgeScope.PROJECT and self.project_id is None:
            raise ValueError("project knowledge requires project_id")
        if self.type is KnowledgeType.SKILL and self.executable_manifest_digest is None:
            raise ValueError("skill knowledge requires a reviewed manifest digest")
        if self.type is not KnowledgeType.SKILL and self.executable_manifest_digest is not None:
            raise ValueError("only skill knowledge can reference an executable manifest")
        return self


class RefinementProposal(StrictModel):
    id: str = Field(min_length=1)
    entry: KnowledgeEntry
    expected_metric: str = Field(min_length=1)
    validation_corpus: str = Field(min_length=1)
    status: ProposalStatus = ProposalStatus.PROPOSED


class ReplayOutcome(StrictModel):
    baseline_success_rate: float = Field(ge=0, le=1)
    candidate_success_rate: float = Field(ge=0, le=1)
    baseline_cost: float = Field(ge=0)
    candidate_cost: float = Field(ge=0)
    preservation_passed: bool
    replay_stable: bool
    evaluated_project_ids: tuple[str, ...] = ()

    @property
    def improved(self) -> bool:
        return bool(
            self.preservation_passed
            and self.replay_stable
            and (
                self.candidate_success_rate > self.baseline_success_rate
                or (
                    self.candidate_success_rate == self.baseline_success_rate
                    and self.candidate_cost < self.baseline_cost
                )
            )
        )


class PromotionRecord(StrictModel):
    proposal_id: str
    status: ProposalStatus
    outcome: ReplayOutcome
    reason: str


class RollbackRecord(StrictModel):
    proposal_id: str
    entry_id: str
    reason: str = Field(min_length=1)


class StoreRecord(StrictModel):
    kind: str
    payload: dict[str, object]


ReplayVerifier = Callable[[RefinementProposal], ReplayOutcome]


class ContinualHarnessStore:
    def __init__(self, path: Path, *, events: EventStore | None = None) -> None:
        self.path = path
        self.events = events
        self._lock = threading.RLock()
        self.proposals: dict[str, RefinementProposal] = {}
        self.entries: dict[str, KnowledgeEntry] = {}
        self.promotions: list[PromotionRecord] = []
        self._load()

    def propose(
        self,
        *,
        entry_type: KnowledgeType,
        scope: KnowledgeScope,
        content: str,
        source_run_id: str,
        trigger_event_ids: tuple[str, ...],
        expected_metric: str,
        validation_corpus: str,
        project_id: str | None = None,
        project_revision_ids: tuple[str, ...] = (),
        environment_digest: str | None = None,
        compatibility: SkillCompatibility | None = None,
        executable_manifest_digest: str | None = None,
    ) -> RefinementProposal:
        entry_payload = {
            "type": entry_type.value,
            "scope": scope.value,
            "content": content,
            "source_run_id": source_run_id,
            "trigger_event_ids": trigger_event_ids,
            "project_id": project_id,
            "project_revision_ids": project_revision_ids,
            "environment_digest": environment_digest,
            "compatibility": (
                None if compatibility is None else compatibility.model_dump(mode="json")
            ),
            "executable_manifest_digest": executable_manifest_digest,
        }
        entry = KnowledgeEntry(id=_stable_id("entry", entry_payload), **entry_payload)
        proposal_payload = {
            "entry": entry.model_dump(mode="json"),
            "expected_metric": expected_metric,
            "validation_corpus": validation_corpus,
        }
        proposal = RefinementProposal(
            id=_stable_id("proposal", proposal_payload),
            entry=entry,
            expected_metric=expected_metric,
            validation_corpus=validation_corpus,
        )
        with self._lock:
            if proposal.id in self.proposals:
                raise ContinualHarnessError("duplicate refinement proposal")
            self.proposals[proposal.id] = proposal
            self._append("proposal", proposal.model_dump(mode="json"))
        if self.events is not None:
            self.events.append(
                EventKind.SKILL_PROPOSED,
                subject_id=proposal.id,
                payload={"entry_type": entry_type.value, "scope": scope.value},
            )
        return proposal

    def evaluate_and_promote(
        self,
        proposal_id: str,
        verifier: ReplayVerifier,
        *,
        benchmark_mode: bool = False,
        minimum_global_projects: int = 3,
    ) -> PromotionRecord:
        with self._lock:
            proposal = self._proposal(proposal_id)
            if proposal.status is not ProposalStatus.PROPOSED:
                raise ContinualHarnessError("proposal has already been decided")
        outcome = verifier(proposal)
        reason = "candidate improved the fixed replay corpus"
        promoted = outcome.improved
        if proposal.entry.scope is KnowledgeScope.GLOBAL:
            unique_projects = len(set(outcome.evaluated_project_ids))
            if benchmark_mode:
                promoted = False
                reason = "benchmark mode disables global promotion"
            elif unique_projects < minimum_global_projects:
                promoted = False
                reason = "global promotion lacks cross-project evidence"
        if not outcome.improved:
            reason = "candidate did not improve while preserving replay stability"
        status = ProposalStatus.PROMOTED if promoted else ProposalStatus.REJECTED
        record = PromotionRecord(
            proposal_id=proposal.id,
            status=status,
            outcome=outcome,
            reason=reason,
        )
        with self._lock:
            decided = proposal.model_copy(update={"status": status})
            self.proposals[proposal.id] = decided
            if promoted:
                self.entries[proposal.entry.id] = proposal.entry
            self.promotions.append(record)
            self._append("promotion", record.model_dump(mode="json"))
        if self.events is not None:
            self.events.append(
                EventKind.SKILL_PROMOTED if promoted else EventKind.SKILL_REJECTED,
                subject_id=proposal.id,
                payload={"reason": reason},
            )
        return record

    def rollback(self, proposal_id: str, *, reason: str) -> RollbackRecord:
        with self._lock:
            proposal = self._proposal(proposal_id)
            if proposal.status is not ProposalStatus.PROMOTED:
                raise ContinualHarnessError("only a promoted proposal can be rolled back")
            record = RollbackRecord(
                proposal_id=proposal.id,
                entry_id=proposal.entry.id,
                reason=reason,
            )
            self.entries.pop(proposal.entry.id, None)
            self.proposals[proposal.id] = proposal.model_copy(
                update={"status": ProposalStatus.ROLLED_BACK}
            )
            self._append("rollback", record.model_dump(mode="json"))
            return record

    def applicable_entries(
        self,
        *,
        run_id: str,
        project_id: str | None,
        engine_family: str,
        environment: EngineEnvironment,
        task_archetype: str,
    ) -> tuple[KnowledgeEntry, ...]:
        applicable: list[KnowledgeEntry] = []
        for entry in self.entries.values():
            if entry.scope is KnowledgeScope.RUN and entry.source_run_id != run_id:
                continue
            if entry.scope is KnowledgeScope.PROJECT and entry.project_id != project_id:
                continue
            if entry.compatibility is not None and not entry.compatibility.matches(
                engine_family=engine_family,
                environment=environment,
                task_archetype=task_archetype,
            ):
                continue
            applicable.append(entry)
        priority = {
            KnowledgeScope.RUN: 0,
            KnowledgeScope.PROJECT: 1,
            KnowledgeScope.GLOBAL: 2,
        }
        return tuple(sorted(applicable, key=lambda item: (priority[item.scope], item.id)))

    def _proposal(self, proposal_id: str) -> RefinementProposal:
        try:
            return self.proposals[proposal_id]
        except KeyError as error:
            raise ContinualHarnessError(f"unknown proposal: {proposal_id}") from error

    def _append(self, kind: str, payload: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = StoreRecord(kind=kind, payload=payload)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(record.model_dump_json() + "\n")

    def _load(self) -> None:
        if not self.path.exists():
            return
        if not self.path.is_file() or self.path.is_symlink():
            raise ContinualHarnessError("continual store is not a regular file")
        for line_number, raw_line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), 1
        ):
            try:
                record = StoreRecord.model_validate_json(raw_line)
                if record.kind == "proposal":
                    proposal = RefinementProposal.model_validate(record.payload)
                    self.proposals[proposal.id] = proposal
                elif record.kind == "promotion":
                    promotion = PromotionRecord.model_validate(record.payload)
                    proposal = self._proposal(promotion.proposal_id)
                    self.proposals[proposal.id] = proposal.model_copy(
                        update={"status": promotion.status}
                    )
                    if promotion.status is ProposalStatus.PROMOTED:
                        self.entries[proposal.entry.id] = proposal.entry
                    self.promotions.append(promotion)
                elif record.kind == "rollback":
                    rollback = RollbackRecord.model_validate(record.payload)
                    proposal = self._proposal(rollback.proposal_id)
                    self.entries.pop(rollback.entry_id, None)
                    self.proposals[proposal.id] = proposal.model_copy(
                        update={"status": ProposalStatus.ROLLED_BACK}
                    )
                else:
                    raise ValueError(f"unknown continual record kind: {record.kind}")
            except (ValueError, ContinualHarnessError) as error:
                raise ContinualHarnessError(
                    f"invalid continual store record at line {line_number}"
                ) from error


class ContinualHarnessError(RuntimeError):
    pass


def _stable_id(prefix: str, payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()}"
