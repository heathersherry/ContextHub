"""Versioned session inputs, evidence and durable checkpoint storage.

Uses the existing tenant-scoped artifact table; no migration or database
connection is created here. A processing/failed checkpoint cannot answer queries.
"""
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator
from contexthub.models.knowledge import Contract, ContractError, Hash, canonical_hash
from .session_state import SessionState, Node
from contexthub.discovery.evidence_units import evidence_unit_view


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    role: Literal["user", "assistant", "system", "tool"]
    content: str


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: str = Field(pattern=r"^src_[0-9a-f]{24}$")
    ordinal: int = Field(strict=True, ge=0)
    source_version: int = Field(strict=True, ge=1)
    timestamp: str | None
    messages: tuple[Message, ...] = Field(min_length=1)
    content_sha256: Hash

    @model_validator(mode="after")
    def content(self):
        if canonical_hash([m.model_dump(mode="json") for m in self.messages]) != self.content_sha256:
            raise ContractError("session_content_hash_mismatch")
        if self.source_id != f"src_{self.ordinal:024x}":
            raise ContractError("session_nonsemantic_id_required")
        return self


class Evidence(Contract):
    id: str
    source_id: str
    source_version: int
    content_sha256: Hash
    message: int
    role: str
    start: int
    end: int
    quote: str


def bind_span(source, message_index, start, end):
    """Bind trusted program offsets, without searching or normalizing source text."""
    if (not 0 <= message_index < len(source.messages)
            or not 0 <= start < end <= len(source.messages[message_index].content)):
        raise ContractError("session_evidence_outside_prefix")
    message = source.messages[message_index]
    data = dict(source_id=source.source_id, source_version=source.source_version,
                content_sha256=source.content_sha256, message=message_index, role=message.role,
                start=start, end=end, quote=message.content[start:end])
    return Evidence(id="e_" + canonical_hash(data), **data)


def extraction_evidence_view(source):
    """Lossless per-message units and a request-local ID -> immutable span registry."""
    units, locations = evidence_unit_view({str(i): m.content for i, m in enumerate(source.messages)})
    registry = {ident: bind_span(source, int(loc["source_id"]), loc["start"], loc["end"])
                for ident, loc in locations.items()}
    view = source.model_dump(mode="json")
    view["messages"] = [dict(role=m.role, evidence_units=units[str(i)])
                        for i, m in enumerate(source.messages)]
    return view, registry


class Vector(Contract):
    node_id: str
    binding: Hash
    values: tuple[float, ...]


class Checkpoint(Contract):
    stream_id: UUID
    account_id: str
    configuration_sha256: Hash
    revision: int = Field(strict=True, ge=0)
    status: Literal["processing", "complete", "unresolved", "failed"]
    sources: tuple[SessionInput, ...] = ()
    state: SessionState = SessionState()
    evidence: tuple[Evidence, ...] = ()
    vectors: tuple[Vector, ...] = ()
    # Previous node versions for audit/history questions; deleted identities
    # are excluded from EVERY version in the answering view.
    history: tuple[Node, ...] = ()
    audits: tuple[dict, ...] = ()
    call_ids: tuple[UUID, ...] = ()
    previous_sha256: Hash | None = None
    attempts: dict[str, dict] = {}
    read_ids: dict[str, tuple[str, ...]] = {}
    source_watches: dict[str, tuple[str, ...]] = {}
    gaps: dict[str, tuple[str, ...]] = {}

    @model_validator(mode="after")
    def validate_sources(self):
        if [s.ordinal for s in self.sources] != list(range(len(self.sources))):
            raise ContractError("session_prefix_not_contiguous")
        registry = {e.id: e for e in self.evidence}
        if len(registry) != len(self.evidence):
            raise ContractError("session_duplicate_evidence")
        for evidence in self.evidence:
            source = next((s for s in self.sources if s.source_id == evidence.source_id), None)
            if source is None:
                raise ContractError("session_evidence_outside_prefix")
            bound = bind_span(source, evidence.message, evidence.start, evidence.end)
            if bound != evidence:
                raise ContractError("session_evidence_binding_mismatch")
        for node in (*self.state.nodes, *self.history):
            if not set(node.evidence) <= registry.keys():
                raise ContractError("session_node_evidence_missing")
        nodes = {n.id for n in self.state.nodes}
        if not (self.attempts.keys() | self.read_ids.keys() | self.source_watches.keys() | self.gaps.keys()) <= nodes:
            raise ContractError("session_wait_target_missing")
        if any(not set(ids) <= nodes for ids in self.read_ids.values()):
            raise ContractError("session_read_memory_missing")
        if any(not set(ids) <= registry.keys() for ids in self.source_watches.values()):
            raise ContractError("session_source_watch_missing")
        return self


def answer_view(checkpoint):
    if checkpoint.status not in ("complete", "unresolved"):
        raise ContractError("session_not_drained")
    current = {n.id: n for n in checkpoint.state.nodes}
    deleted = {i for i, n in current.items() if n.status == "deleted"}
    # Never expose raw transcripts, rules, stale values, evidence or audit traces
    # as current knowledge. History carries an explicit historical designation.
    sources = {s.source_id: s for s in checkpoint.sources}
    evidence = {e.id: e for e in checkpoint.evidence}
    def visible(node):
        return dict(id=node.id, version=node.version, text=node.text,
                    source_times=list(dict.fromkeys(sources[evidence[e].source_id].timestamp
                                                    for e in node.evidence)))
    history = [visible(n) for n in checkpoint.history
               if n.id not in deleted and current[n.id].status == "fresh"
               and n.kind != "rule" and n.status == "fresh"]
    return dict(current=[visible(n) for n in current.values() if n.status == "fresh" and n.kind != "rule"],
                historical=history,
                unavailable=[n.id for n in current.values() if n.status in ("stale", "unresolved")])


class PgSessionStore:
    """Immutable checkpoint chain, serialized CAS per isolated stream."""
    def __init__(self, repository, account_id, stream_id):
        self.repository, self.account_id, self.stream_id = repository, account_id, stream_id

    async def _load(self, db):
        row = await db.fetchrow(
            """SELECT sha256,payload FROM knowledge_artifacts
            WHERE account_id=$1 AND kind='session_checkpoint' AND payload->>'stream_id'=$2
            ORDER BY (payload->>'revision')::bigint DESC LIMIT 1""",
            self.account_id, str(self.stream_id))
        if row is None:
            return None
        if canonical_hash(row["payload"]) != row["sha256"]:
            raise ContractError("session_checkpoint_hash_mismatch")
        checkpoint = Checkpoint.model_validate(row["payload"])
        if checkpoint.account_id != self.account_id or checkpoint.stream_id != self.stream_id:
            raise ContractError("session_checkpoint_scope_mismatch")
        return checkpoint

    async def load(self):
        async with self.repository.session(self.account_id) as db:
            return await self._load(db)

    async def assert_empty(self):
        async with self.repository.session(self.account_id) as db:
            # An episode owns its whole maintenance account, not merely a key.
            if await db.fetchval("SELECT EXISTS(SELECT 1 FROM knowledge_artifacts WHERE account_id=$1)", self.account_id):
                raise ContractError("session_account_not_empty")
            if await db.fetchval("SELECT EXISTS(SELECT 1 FROM contexts WHERE account_id=$1)", self.account_id):
                raise ContractError("session_account_not_empty")
            if await db.fetchval("SELECT EXISTS(SELECT 1 FROM execution_attempts WHERE account_id=$1)", self.account_id):
                raise ContractError("session_account_not_empty")

    async def save(self, checkpoint, expected):
        checkpoint = Checkpoint.model_validate(checkpoint.model_dump(mode="json"))
        if checkpoint.account_id != self.account_id or checkpoint.stream_id != self.stream_id:
            raise ContractError("session_checkpoint_scope_mismatch")
        async with self.repository.session(self.account_id) as db:
            await db.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                             self.account_id + ":" + str(self.stream_id))
            previous = await self._load(db)
            if (canonical_hash(previous) if previous else None) != expected:
                raise ContractError("session_checkpoint_cas_conflict")
            if (checkpoint.previous_sha256 != expected
                    or checkpoint.revision != (previous.revision + 1 if previous else 0)):
                raise ContractError("session_checkpoint_sequence_mismatch")
            await db.execute("""INSERT INTO knowledge_artifacts
                (account_id,id,kind,schema_version,sha256,payload) VALUES($1,$2,$3,1,$4,$5)""",
                self.account_id, uuid5(self.stream_id, f"checkpoint:{checkpoint.revision}"),
                "session_checkpoint", canonical_hash(checkpoint), checkpoint.model_dump(mode="json"))
