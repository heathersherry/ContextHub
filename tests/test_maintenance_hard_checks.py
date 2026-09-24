"""S6 structural contracts supplement the actual PostgreSQL pipeline tests."""

import json
from types import SimpleNamespace
import pytest
from contexthub.models.knowledge import ContractError
from contexthub.models.maintenance import CheckItem, HardCheckResult
from contexthub.propagation.hard_checks import passed, REQUIRED_CHECKS, CHECKER_VERSION
from contexthub.propagation.verifier import parse_verdict
from s6_helpers import sample_data, verdict_payload
from uuid import uuid4
from knowledge_helpers import HASH


@pytest.mark.parametrize("verdict", ["FAIL", "UNKNOWN"])
@pytest.mark.parametrize("code", REQUIRED_CHECKS)
def test_no_required_hard_check_can_be_overridden(code, verdict):
    check = HardCheckResult(
        account_id="fixture",
        id=uuid4(),
        candidate=dict(account_id="fixture", id=uuid4(), kind="candidate", sha256=HASH),
        checker_version=CHECKER_VERSION,
        checks=tuple(
            CheckItem(
                code=k, verdict=verdict if k == code else "PASS", reason="fixture"
            )
            for k in REQUIRED_CHECKS
        ),
    )
    assert not passed(check)


@pytest.mark.parametrize("fault", ["claim", "cause", "empty_evidence"])
def test_verifier_requires_checkable_complete_references(fault):
    data = sample_data()
    payload = verdict_payload(data)
    if fault == "claim":
        payload["complete_support"]["claim_indexes"] = []
    elif fault == "cause":
        payload["conflicts_resolved"]["cause_ids"] = []
    else:
        payload["applicability"]["evidence"] = []
    with pytest.raises(ContractError):
        parse_verdict(
            json.dumps(payload),
            SimpleNamespace(data=data),
            SimpleNamespace(
                read=[SimpleNamespace(evidence=e) for e in data.claims[0].evidence]
            ),
        )
