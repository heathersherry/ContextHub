from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock
import pytest
from pydantic import ValidationError
from contexthub.models.knowledge import ContextVersion, ContractError
from contexthub.propagation.contracts import RequiredInput
from contexthub.propagation.evidence import (
    check_progress,
    chunks_for,
    required_manifest,
)
from contexthub.services.propagation_engine import PropagationEngine


def test_missing_input_remains_required_without_fabricated_evidence():
    missing = RequiredInput(
        source=ContextVersion(context_id=uuid4(), version=7),
        role="rule",
        rule_id="r",
        rule_version="1",
    )
    frame = SimpleNamespace(required=(missing,), history=())
    assert required_manifest(frame, "M1") == (missing,)
    assert required_manifest(frame, "M2") == (missing,)
    assert not check_progress(1, 2, [], [], [], [])
    with pytest.raises(ValidationError):
        RequiredInput(source=missing.source, role="rule")
    with pytest.raises(ValidationError):
        RequiredInput(source=missing.source, role="before", gold="leak")


@pytest.mark.parametrize(
    "read,pages,chunks",
    [
        ([0], [0], [(0, 0, 5)]),
        ([0, 1], [0, 0], [(0, 0, 5), (1, 0, 5)]),
        ([0, 1], [1, 0], [(0, 0, 5), (1, 0, 5)]),
        ([0, 1], [0, 1], [(0, 0, 5)]),
        ([0, 1], [0, 1], [(0, 0, 5), (0, 0, 5)]),
        ([1, 0], [0, 1], [(0, 0, 5), (1, 0, 5)]),
    ],
)
def test_progress_rejects_missing_duplicate_out_of_order(read, pages, chunks):
    assert not check_progress(2, 1, read, pages, [(0, 0, 5), (1, 0, 5)], chunks)


def test_pages_and_chunks_complete_only_at_end():
    chunks = ((0, 0, 5), (1, 0, 5))
    assert check_progress(2, 1, [0, 1], [0, 1], chunks, chunks)
    assert chunks_for(11, 0, None, 4) == ((0, 4), (4, 8), (8, 11))
    with pytest.raises(ContractError):
        chunks_for(5, 0, 8, 4)


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [True, False])
async def test_s5_dispatch_excludes_legacy_even_when_executor_missing(configured):
    handler = AsyncMock() if configured else None
    engine = PropagationEngine(
        None, None, "unused", None, None, maintenance_executor=handler
    )
    engine._trace = AsyncMock()
    engine._finish_event = AsyncMock()
    engine._fetch_dependents = AsyncMock(side_effect=AssertionError("legacy path"))
    event = {
        "event_id": uuid4(),
        "change_type": "modified",
        "metadata": {"s5_watches": [{}]},
    }
    await engine._process_claimed_event(event)
    engine._fetch_dependents.assert_not_called()
    if configured:
        handler.handle_event.assert_awaited_once_with(event)
    assert engine._finish_event.call_args.kwargs["success"] is configured


def test_frozen_layout_binds_source_span_role_and_pagination():
    """Same-size lists are insufficient; empty inputs still have read receipts."""
    from contexthub.models.knowledge import EvidenceRef, SealRef, text_hash
    from contexthub.models.maintenance import EvidenceBundle, ReadEntry
    from contexthub.propagation.evidence import (
        EVIDENCE_ORDERING,
        validate_receipt_bundle,
    )

    def ref(kind):
        return SealRef(account_id="layout-test", id=uuid4(), kind=kind, sha256="a" * 64)

    source, empty = (ContextVersion(context_id=uuid4(), version=1) for _ in range(2))
    source_ref, empty_ref, maintenance = (
        ref("source"),
        ref("source"),
        ref("maintenance"),
    )
    required = (
        RequiredInput(source=source, role="before"),
        RequiredInput(source=empty, role="alternative"),
    )
    frame = SimpleNamespace(
        required=required,
        snapshot=maintenance,
        history=(
            SimpleNamespace(
                source=source, content_level="l2_content", length=3, seal=source_ref
            ),
            SimpleNamespace(
                source=empty, content_level="l2_content", length=0, seal=empty_ref
            ),
        ),
    )
    policy = SimpleNamespace(content_level="l2_content", page_size=1, chunk_size=2)
    receipt = SimpleNamespace(
        scope="M1",
        required=required,
        read_ordinals=(0, 1),
        pages=(0, 1),
        chunks=((0, 0, 2), (0, 2, 3)),
    )
    text = "漢🦋e"
    entries = tuple(
        ReadEntry(
            ordinal=i,
            page=0,
            chunk=i,
            evidence=EvidenceRef(
                source=source_ref,
                start=a,
                end=b,
                excerpt=text[a:b],
                excerpt_sha256=text_hash(text[a:b]),
                provenance=("before",),
            ),
        )
        for i, (a, b) in enumerate(((0, 2), (2, 3)))
    )
    bundle = EvidenceBundle(
        account_id="layout-test",
        id=uuid4(),
        maintenance=maintenance,
        scope_id="M1",
        required=entries,
        read=entries,
        complete=True,
        truncated=False,
        ordering=EVIDENCE_ORDERING,
        page_size=1,
        chunk_size=2,
    )
    validate_receipt_bundle(frame, receipt, policy, bundle)
    for change in (
        {"page_size": 2},
        {"chunk_size": 3},
        {"ordering": "other"},
        {"truncated": True},
        {"next_cursor": "unfinished"},
    ):
        with pytest.raises(ContractError, match="bundle_reference_mismatch"):
            validate_receipt_bundle(
                frame, receipt, policy, bundle.model_copy(update=change)
            )
    # Two equal, locally valid lists can still use the wrong source or role.
    for change in ({"source": empty_ref}, {"provenance": ("after",)}):
        row = entries[0].model_copy(
            update={"evidence": entries[0].evidence.model_copy(update=change)}
        )
        bad = (row, entries[1])
        with pytest.raises(ContractError, match="bundle_manifest_mismatch"):
            validate_receipt_bundle(
                frame,
                receipt,
                policy,
                bundle.model_copy(update={"required": bad, "read": bad}),
            )
    receipt.read_ordinals = (0,)  # Empty is not silently omitted from required/read.
    with pytest.raises(ContractError, match="incomplete_manifest"):
        validate_receipt_bundle(frame, receipt, policy, bundle)
