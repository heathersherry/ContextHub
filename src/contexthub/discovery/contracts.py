"""S4 audit envelope; S2 GraphSnapshot bytes and hashes remain unchanged."""

import json
from typing import Literal
from uuid import UUID
from pydantic import model_validator
from contexthub.models.knowledge import (
    Sealed,
    SealRef,
    Hash,
    ContextVersion,
    EvidenceRef,
    Contract,
    Name,
    ContractError,
    canonical_hash,
)
from contexthub.discovery.search import CandidatePage, SearchIssue
from contexthub.discovery.adjudication import Adjudication


def encode_configuration(config: Contract) -> dict:
    """Lossless S4 envelope: jsonb must not reinterpret nested JSON numbers.

    Keep the runtime config/hash unchanged. Only new report payloads use this
    explicitly versioned representation; legacy report dicts remain readable.
    """
    return {
        "encoding": "canonical-json-text-v1",
        "payload": json.dumps(
            config.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ),
    }


class NodeMapping(Contract):
    candidate_id: UUID
    proposition_id: UUID | None
    node: ContextVersion
    text: str
    evidence: tuple[EvidenceRef, ...]


class Decision(Contract):
    upstream_id: UUID
    downstream_id: UUID
    result: Adjudication


class DiscoveryReport(Sealed):
    kind: Literal["discovery_report"] = "discovery_report"
    execution_id: UUID
    code_version: Name
    config_sha256: Hash
    configuration: dict
    prior_extractions: tuple[SealRef, ...] = ()
    shared_extraction: SealRef
    graph: SealRef
    history_sha256: Hash
    strategy: Literal["selective", "full-history"]
    status: Literal["complete", "pending"]
    states: tuple[
        Literal[
            "awaiting_extraction",
            "awaiting_search",
            "awaiting_adjudication",
            "complete",
            "pending",
        ],
        ...,
    ]
    reasons: tuple[Name, ...]
    mappings: tuple[NodeMapping, ...]
    pages: tuple[CandidatePage, ...]
    evidence_recoveries: tuple[SearchIssue, ...]
    decisions: tuple[Decision, ...]
    call_ids: tuple[UUID, ...]

    def configuration_payload(self) -> dict:
        """Return the original configuration with its original numeric types."""
        payload = self.configuration
        if "encoding" in payload:
            if (
                set(payload) != {"encoding", "payload"}
                or payload["encoding"] != "canonical-json-text-v1"
            ):
                raise ContractError("configuration_encoding_unknown")
            try:
                payload = json.loads(payload["payload"])
            except (ValueError, TypeError) as exc:
                raise ContractError("configuration_payload_invalid") from exc
        if (
            not isinstance(payload, dict)
            or canonical_hash(payload) != self.config_sha256
        ):
            raise ContractError("configuration_hash_mismatch")
        return payload

    @model_validator(mode="after")
    def completion(self):
        self.configuration_payload()
        if self.status == "complete" and self.reasons:
            raise ContractError("complete_discovery_has_errors")
        if self.status == "pending" and not self.reasons:
            raise ContractError("pending_reason_required")
        return self


class GraphHandoff(Sealed):
    """Complete ordered discovery history, without re-extraction or graph inference."""

    kind: Literal["graph_handoff"] = "graph_handoff"
    execution_id: UUID
    agent_id: Name
    scope: Name
    owner_space: Name
    config_sha256: Hash
    configuration: dict
    strategy: Literal["selective", "full-history"]
    reports: tuple[SealRef, ...]
    prepared: tuple[SealRef, ...]
    sources: tuple[ContextVersion, ...]
    graph: SealRef
    mappings: tuple[NodeMapping, ...]
    status: Literal["complete", "pending"]
    reasons: tuple[Name, ...]

    configuration_payload = DiscoveryReport.configuration_payload
    completion = model_validator(mode="after")(DiscoveryReport.completion)
