"""New session protocol control plane. No database/client construction here.

The legacy phased runners are intentionally not silently reinterpreted. A
production backend must implement this boundary with atomic durable receipts;
session_factory composes the new durable service without changing the legacy CLI.
"""

from copy import deepcopy
from typing import Protocol

from contexthub.models.knowledge import ContractError
from .chronology import QuestionVault, prefix_hash
from .firewall import validate_maintenance_payload
from .schemas import MaintenanceInput, MaintenanceSource, canonical_sha256


class SessionBackend(Protocol):
    async def assert_empty(self) -> None:
        """Reject reused episode state, including indexes and pending work."""

    async def ingest_session(self, source: MaintenanceSource) -> dict:
        """Store full session; finish delta discovery/invalidation/maintenance.

        Return {ordinal, source_id, source_version, content_sha256,
        status: complete|unresolved, revision, state_sha256}. 'Unresolved' is a
        drained safe state, not running work or a successful recovery claim.
        No future sessions, boundaries, questions or feedback are supplied.
        """

    async def snapshot(self) -> dict:
        """Detached complete maintenance state, including versions/index identity."""


class SessionEpisodeRunner:
    def __init__(self, backend: SessionBackend, answerer):
        self.backend, self.answerer, self.used = backend, answerer, False

    async def run(self, runtime, boundary, *, question_directory):
        if self.used or type(runtime) is not MaintenanceInput:
            raise ContractError("session_runner_requires_fresh_episode")
        runtime = validate_maintenance_payload(runtime.model_dump(mode="json"))
        boundary.check(runtime)
        self.used = True
        await self.backend.assert_empty()
        vault = QuestionVault(question_directory, boundary)
        receipts, answers, seals = [], {}, {}
        self.progress = dict(protocol="session-evidence-v2", sessions=receipts,
                             states=seals, answers=answers, judge_executed=False)
        revision = -1
        for source in runtime.sources[: boundary.after.ordinal + 1]:
            # One immutable, allowlisted session; never the full episode.
            # S1 IDs hash raw session names. Do not expose even that dictionary
            # lookup handle to maintenance. Ordinals are local to isolated memory.
            visible = source.model_copy(update={"source_id": f"src_{source.ordinal:024x}"})
            receipt = await self.backend.ingest_session(visible)
            expected = {k: getattr(visible, k) for k in (
                "ordinal", "source_id", "source_version", "content_sha256")}
            if any(receipt.get(k) != v for k, v in expected.items()):
                raise ContractError("session_receipt_source_mismatch")
            if (receipt.get("status") not in ("complete", "unresolved")
                    or type(receipt.get("revision")) is not int
                    or receipt["revision"] <= revision):
                raise ContractError("session_not_drained")
            state = deepcopy(await self.backend.snapshot())
            if receipt.get("state_sha256") != canonical_sha256(state):
                raise ContractError("session_receipt_state_mismatch")
            revision = receipt["revision"]
            receipts.append(deepcopy(receipt))
            for phase in ("before", "after"):
                if source.ordinal != getattr(boundary, phase).ordinal:
                    continue
                sealed = dict(phase=phase, episode_id=runtime.episode_id,
                              prefix_sha256=prefix_hash(runtime, source.ordinal),
                              state=state, revision=revision)
                sealed["sha256"] = canonical_sha256(sealed)
                # Only the answer capability gets questions. It gets detached
                # bytes, no backend, mutation tools, history writer or scorer.
                bundle = vault.release(phase, sealed)
                rows = []
                for question in bundle.questions:
                    try:
                        text = await self.answerer(question.text, deepcopy(sealed))
                        if not isinstance(text, str):
                            raise TypeError("answer must be text")
                        row = dict(question_id=question.question_id, answer=text, error=None)
                    except Exception:
                        # Failed slots remain in the original denominator/order.
                        row = dict(question_id=question.question_id, answer="", error="answer_failed")
                    row["state_sha256"] = sealed["sha256"]
                    rows.append(row)
                    if canonical_sha256(await self.backend.snapshot()) != receipt["state_sha256"]:
                        raise ContractError("answer_modified_maintenance_state")
                answers[phase], seals[phase] = rows, sealed
        if set(answers) != {"before", "after"}:
            raise ContractError("session_question_boundary_missing")
        return dict(protocol="session-evidence-v2", sessions=receipts,
                    states=seals, answers=answers, judge_executed=False)
