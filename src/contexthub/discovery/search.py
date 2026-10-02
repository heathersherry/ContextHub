"""Bounded, manifest-scoped candidate discovery. No dependency verdicts or writes."""

from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID, uuid5

from pydantic import AwareDatetime, Field, model_validator

from contexthub.models.knowledge import (
    Contract,
    ContractError,
    ContextVersion,
    Hash,
    Name,
    Nonnegative,
    Positive,
    SealRef,
    SourceSnapshot,
    PropositionArtifact,
    EvidenceRef,
    canonical_hash,
)
from contexthub.services.artifact_service import ArtifactService
from contexthub.services.evidence_service import EvidenceService
from ._work import LocalOperation, MeteredDB
from .extraction import error_code


class HistoryEntry(Contract):
    source: SealRef
    ingestion_ordinal: Nonnegative


class ExplicitProvenance(Contract):
    proposition_id: UUID
    source: SealRef
    reason: Name


class HistorySnapshot(Contract):
    account_id: Name
    entries: tuple[HistoryEntry, ...]
    cutoff_at: AwareDatetime
    cutoff_ordinal: Nonnegative
    provenance: tuple[ExplicitProvenance, ...] = ()

    @model_validator(mode="after")
    def manifest(self):
        if len({e.source.id for e in self.entries}) != len(self.entries):
            raise ContractError("duplicate_history_source")
        if any(
            e.source.account_id != self.account_id or e.source.kind != "source"
            for e in self.entries
        ):
            raise ContractError("invalid_history_source")
        return self


class SearchPolicy(Contract):
    id: Name
    strategy: Literal["full-history", "selective"]
    initial_sources: Positive
    expansion_factor: int = Field(strict=True, ge=2)
    page_size: Positive
    ordering: Literal["ingestion_asc", "ingestion_desc"]
    # Identity only at S3: it NEVER gates required reads or expansion.
    lambda_p1: float = Field(ge=0, allow_inf_nan=False)

    @classmethod
    def load(cls, path: str | Path):
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


class SearchIssue(Contract):
    code: Name
    source: SealRef | None = None


class IndexSelection(Contract):
    sources: tuple[SealRef, ...]
    complete: bool
    truncated: bool
    issues: tuple[SearchIssue, ...] = ()


class CandidateIndex(Protocol):
    async def select(
        self, db, artifact: PropositionArtifact, scope: tuple[SealRef, ...]
    ) -> IndexSelection: ...


class SearchCandidate(Contract):
    id: UUID
    kind: Literal["batch_proposition", "history_source"]
    proposition_id: UUID | None
    sources: tuple[SealRef, ...]
    source_versions: tuple[ContextVersion, ...]
    text: str
    evidence: tuple[EvidenceRef, ...]
    explicit_provenance: tuple[ExplicitProvenance, ...]


class SearchCursor(Contract):
    binding: Hash
    scope_size: Nonnegative
    offset: Nonnegative
    emitted_propositions: tuple[UUID, ...]
    read_sources: tuple[SealRef, ...]
    issues: tuple[SearchIssue, ...]
    expansion_reasons: tuple[Name, ...]
    bypass_index: bool
    page_no: Nonnegative


class CandidatePage(Contract):
    artifact: SealRef
    history_sha256: Hash
    policy_sha256: Hash
    candidates: tuple[SearchCandidate, ...]
    allowed_sources: tuple[SealRef, ...]
    scope_sources: tuple[SealRef, ...]
    read_sources: tuple[SealRef, ...]
    ordering: Name
    cursor: SearchCursor | None
    complete: bool
    truncated: bool
    issues: tuple[SearchIssue, ...]
    expansion_reasons: tuple[Name, ...]
    at_max_scope: bool
    page_no: Nonnegative

    @model_validator(mode="after")
    def completion(self):
        if self.complete and (self.cursor or self.truncated or self.issues):
            raise ContractError("invalid_candidate_completion")
        return self


class EvidenceRead(Contract):
    evidence: tuple[EvidenceRef, ...]
    recoveries: tuple[SearchIssue, ...]


class SearchService:
    def __init__(
        self, *, recorder, artifacts=None, index: CandidateIndex | None = None
    ):
        self.recorder = recorder
        self.artifacts = artifacts or ArtifactService()
        self.evidence = EvidenceService(self.artifacts)
        self.index = index

    async def _admit_source(self, db, source, history, snapshots):
        """Every path to a body read uses the same history-specific boundary.

        Only successfully admitted immutable snapshots enter the page cache.
        A failed metadata read never grants permission to read the body later.
        """
        entry = next((e for e in history.entries if e.source == source), None)
        if entry is None or source.account_id != history.account_id:
            raise ContractError("source_outside_scope")
        if entry.ingestion_ordinal > history.cutoff_ordinal:
            raise ContractError("source_after_ingestion_cutoff")
        snapshot = snapshots.get(source)
        if snapshot is None:
            snapshot = await self.artifacts.get(db, source)
        if not isinstance(snapshot, SourceSnapshot):
            raise ContractError("reference_kind_mismatch")
        if snapshot.ingested_at > history.cutoff_at:
            raise ContractError("source_after_cutoff")
        snapshots[source] = snapshot
        return snapshot

    async def search(
        self,
        db,
        artifact: SealRef,
        history: HistorySnapshot,
        policy: SearchPolicy,
        *,
        identity,
        cursor: SearchCursor | None = None,
    ) -> CandidatePage:
        if (
            identity.account_id != history.account_id
            or artifact.account_id != history.account_id
        ):
            raise ContractError("tenant_mismatch")
        if identity.config_sha256 != canonical_hash(policy):
            raise ContractError("search_config_mismatch")
        binding = canonical_hash(
            {
                "artifact": artifact.model_dump(mode="json"),
                "history": history.model_dump(mode="json"),
                "policy": policy.model_dump(mode="json"),
            }
        )
        if cursor and cursor.binding != binding:
            raise ContractError("cursor_scope_mismatch")
        work = LocalOperation(
            self.recorder,
            identity,
            {
                "binding": binding,
                "cursor": cursor.model_dump(mode="json") if cursor else None,
            },
        )
        await work.start()
        db = MeteredDB(db, work)
        try:
            page = await self._page(
                db, artifact, history, policy, cursor, binding, work
            )
        except Exception as exc:
            await work.finish({"binding": binding}, error_code(exc))
            raise
        await work.finish(page, page.issues[0].code if page.issues else None)
        return page

    async def expand(self, db, pages, history, policy, *, identity, reason):
        """Resume a completed local scope under the SAME manifest and policy.

        No new source is authorized. Previously emitted IDs are retained; every
        new/recovered body read still passes _admit_source. At max returns None.
        """
        if not pages:
            raise ContractError("expansion_requires_pages")
        last = pages[-1]
        if last.cursor is not None:
            raise ContractError("consume_cursor_before_expansion")
        if any(p.artifact != last.artifact or p.history_sha256 != canonical_hash(history)
               or p.policy_sha256 != canonical_hash(policy) for p in pages):
            raise ContractError("expansion_identity_mismatch")
        if last.at_max_scope:
            return None
        entries = sorted((e for e in history.entries if e.ingestion_ordinal <= history.cutoff_ordinal),
                         key=lambda e: (e.ingestion_ordinal, str(e.source.id)),
                         reverse=policy.ordering == "ingestion_desc")
        size = min(len(entries), max(len(last.scope_sources) + 1,
                                    len(last.scope_sources) * policy.expansion_factor))
        binding = canonical_hash({"artifact": last.artifact.model_dump(mode="json"),
            "history": history.model_dump(mode="json"), "policy": policy.model_dump(mode="json")})
        cursor = SearchCursor(binding=binding, scope_size=size, offset=0,
            emitted_propositions=tuple(dict.fromkeys(c.id for p in pages for c in p.candidates
                                                    if c.kind == "batch_proposition")),
            read_sources=last.read_sources, issues=(),
            expansion_reasons=tuple(dict.fromkeys((*last.expansion_reasons, reason))),
            bypass_index=False, page_no=last.page_no + 1)
        return await self.search(db, last.artifact, history, policy, identity=identity, cursor=cursor)

    async def read_evidence(self, db, refs, history):
        """History-admitted whole-source evidence for S4, including recovery."""
        snapshots, result, recoveries = {}, [], []
        for ref in refs:
            try:
                snapshot = await self._admit_source(db, ref, history, snapshots)
            except Exception as exc:
                # Same single direct metadata recovery as S3's max-scope pass.
                # Failure grants NO body permission; re-admit from scratch.
                recoveries.append(SearchIssue(code=error_code(exc),source=ref))
                snapshot = await self._admit_source(db, ref, history, {})
            text = await self.evidence.read_source(db, snapshot)
            if text:
                result.append(await self.evidence.reference(db, ref, start=0, end=len(text),
                                                           provenance=("manifest-scoped source read",)))
        return EvidenceRead(evidence=tuple(result),recoveries=tuple(recoveries))

    async def _page(self, db, ref, history, policy, cursor, binding, work):
        work.add("retrieval_pages")
        obj = await self.artifacts.get(db, ref)
        if not isinstance(obj, PropositionArtifact):
            raise ContractError("reference_kind_mismatch")
        issues = list(cursor.issues if cursor else ())
        reasons = list(cursor.expansion_reasons if cursor else ())
        read = list(cursor.read_sources if cursor else ())
        page_no = cursor.page_no if cursor else 0
        emitted = list(cursor.emitted_propositions if cursor else ())
        bypass = cursor.bypass_index if cursor else False
        offset = cursor.offset if cursor else 0
        permanent = []
        if obj.status not in ("complete", "partial"):
            permanent.append(SearchIssue(code="extraction_pending"))
        entries = sorted(
            history.entries,
            key=lambda e: (e.ingestion_ordinal, str(e.source.id)),
            reverse=policy.ordering == "ingestion_desc",
        )
        planned, snapshots = [], {}
        # Validate the complete manifest before accessing content. Database wall
        # clock alone cannot admit a future session with an earlier timestamp.
        for entry in entries:
            source = entry.source
            if entry.ingestion_ordinal > history.cutoff_ordinal:
                permanent.append(
                    SearchIssue(code="source_after_ingestion_cutoff", source=source)
                )
                continue
            # Pagination is based on the explicit manifest, not on which
            # metadata reads happened to succeed on this page. Keep rejected
            # entries as work slots, but never treat a slot as read permission.
            planned.append(source)
            try:
                await self._admit_source(db, source, history, snapshots)
            except Exception as exc:
                if error_code(exc) == "source_after_cutoff":
                    permanent.append(
                        SearchIssue(code="source_after_cutoff", source=source)
                    )
                else:
                    issues.extend(
                        (
                            SearchIssue(code=error_code(exc), source=source),
                            SearchIssue(
                                code="source_metadata_read_failed", source=source
                            ),
                        )
                    )
        forced = list(obj.sources)
        prop_ids = {p.id for p in obj.propositions}
        for prov in history.provenance:
            if prov.proposition_id not in prop_ids:
                permanent.append(
                    SearchIssue(
                        code="provenance_proposition_missing", source=prov.source
                    )
                )
            if prov.source not in forced:
                forced.append(prov.source)
        for source in forced:
            if source not in planned:
                permanent.append(
                    SearchIssue(code="source_outside_scope", source=source)
                )
        size = (
            cursor.scope_size
            if cursor
            else (
                len(planned)
                if policy.strategy == "full-history"
                else min(policy.initial_sources, len(planned))
            )
        )
        if size > len(planned):
            raise ContractError("cursor_scope_mismatch")
        scope = tuple(s for s in planned if s in planned[:size] or s in forced)
        at_max = len(scope) == len(planned)
        selected = scope
        # Index is optional and never authoritative about the allowed boundary.
        # A complete page must enumerate the requested scope. Faults cause a
        # finite expansion and then a direct source-table fallback at max scope.
        if self.index and not bypass and policy.strategy == "selective":
            try:
                # The query artifact also contains source-derived text; admit
                # its batch before exposing it to an external index adapter.
                for source in obj.sources:
                    await self._admit_source(db, source, history, snapshots)
                # An index may itself read content. Do not hand it sources whose
                # metadata is missing or whose history cutoff check failed.
                index_scope = tuple(s for s in scope if s in snapshots)
                selection = await self.index.select(db, obj, index_scope)
                if any(s not in index_scope for s in selection.sources):
                    raise ContractError("index_outside_scope")
                if (
                    not selection.complete
                    or selection.truncated
                    or selection.issues
                    or len(selection.sources) != len(set(selection.sources))
                    or set(selection.sources) != set(index_scope)
                ):
                    issues.extend(selection.issues)
                    issues.append(
                        SearchIssue(
                            code="index_truncated"
                            if selection.truncated
                            else "index_incomplete"
                        )
                    )
                selected = tuple(s for s in scope if s in selection.sources)
            except Exception as exc:
                issues.append(SearchIssue(code="index_error_" + error_code(exc)))
                selected = ()
        candidates = []
        # A fixed sequence for this scope: the whole extraction batch, then
        # source candidates. Page size bounds both kinds; IDs survive expansion.
        units = (
            [("batch", p) for p in obj.propositions] if obj.status in ("complete", "partial") else []
        )
        units += [("source", s) for s in scope]
        chunk = units[offset : offset + policy.page_size]
        for kind, item in chunk:
            if kind == "batch":
                prop = item
                if prop.id in emitted:
                    continue
                sources = tuple(dict.fromkeys(e.source for e in prop.evidence))
                source = sources[0]
                try:
                    # Admit every evidence source before resolving any body in
                    # the proposition, including metadata recovered this page.
                    for source in sources:
                        await self._admit_source(db, source, history, snapshots)
                    for evidence in prop.evidence:
                        source = evidence.source
                        await self.evidence.resolve(db, evidence)
                    candidates.append(
                        SearchCandidate(
                            id=prop.id,
                            kind="batch_proposition",
                            proposition_id=prop.id,
                            sources=sources,
                            source_versions=tuple(snapshots[s].source for s in sources),
                            text=prop.text,
                            evidence=prop.evidence,
                            explicit_provenance=tuple(
                                p
                                for p in history.provenance
                                if p.proposition_id == prop.id
                            ),
                        )
                    )
                    emitted.append(prop.id)
                except Exception as exc:
                    issues.append(SearchIssue(code=error_code(exc), source=source))
                continue
            source = item
            if source in read or source not in selected:
                continue
            try:
                snapshot = await self._admit_source(db, source, history, snapshots)
                text = await self.evidence.read_source(db, snapshot)
                read.append(source)
                candidates.append(
                    SearchCandidate(
                        id=uuid5(source.id, "history-source"),
                        kind="history_source",
                        proposition_id=None,
                        sources=(source,),
                        source_versions=(snapshot.source,),
                        text=text,
                        evidence=(),
                        explicit_provenance=tuple(
                            p for p in history.provenance if p.source == source
                        ),
                    )
                )
            except Exception as exc:
                issues.append(SearchIssue(code=error_code(exc), source=source))
        offset += len(chunk)
        issues = list(dict.fromkeys(issues))
        next_cursor = None
        if offset < len(units):
            next_cursor = SearchCursor(
                binding=binding,
                scope_size=size,
                offset=offset,
                emitted_propositions=tuple(emitted),
                read_sources=tuple(read),
                issues=tuple(issues),
                expansion_reasons=tuple(reasons),
                bypass_index=bypass,
                page_no=page_no + 1,
            )
        elif issues or permanent:
            reasons.extend(
                i.code for i in (*issues, *permanent) if i.code not in reasons
            )
            # At maximum, index/metadata faults get exactly one direct pass.
            # The same history boundary applies during this recovery pass.
            can_expand = not at_max
            can_fallback = not bypass and (
                (self.index is not None and policy.strategy == "selective")
                or any(i.code == "source_metadata_read_failed" for i in issues)
            )
            if can_expand or can_fallback:
                next_size = min(
                    len(planned), max(size + 1, size * policy.expansion_factor)
                )
                next_cursor = SearchCursor(
                    binding=binding,
                    scope_size=next_size,
                    offset=0,
                    emitted_propositions=tuple(emitted),
                    read_sources=tuple(read),
                    issues=(),
                    expansion_reasons=tuple(reasons),
                    bypass_index=at_max or bypass,
                    page_no=page_no + 1,
                )
        all_issues = tuple(dict.fromkeys((*permanent, *issues)))
        complete = (
            next_cursor is None and not all_issues and all(s in read for s in scope)
        )
        return CandidatePage(
            artifact=ref,
            history_sha256=canonical_hash(history),
            policy_sha256=canonical_hash(policy),
            candidates=tuple(candidates),
            allowed_sources=tuple(s for s in planned if s in snapshots),
            scope_sources=scope,
            read_sources=tuple(read),
            ordering=policy.ordering,
            cursor=next_cursor,
            complete=complete,
            truncated=any(
                i.code in ("index_truncated", "index_incomplete") for i in issues
            ),
            issues=all_issues,
            expansion_reasons=tuple(reasons),
            at_max_scope=at_max,
            page_no=page_no,
        )
