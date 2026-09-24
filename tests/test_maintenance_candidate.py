import json
import pytest
from pydantic import ValidationError
from contexthub.propagation.candidate import GenerationResponse, ProposalData
from s6_helpers import sample_data


@pytest.mark.parametrize(
    "field",
    [
        "PASS",
        "confidence",
        "fresh",
        "gold",
        "question",
        "task_type",
        "hop",
        "path_labels",
    ],
)
def test_generator_self_grading_and_privileged_fields_rejected(field):
    raw = dict(proposal=sample_data().model_dump(mode="json"), unresolved_reason=None)
    raw[field] = "PASS"
    with pytest.raises(ValidationError):
        GenerationResponse.model_validate_json(json.dumps(raw))


def test_no_candidate_is_explicit_not_empty_success():
    assert (
        GenerationResponse(proposal=None, unresolved_reason="no evidence").proposal
        is None
    )
    with pytest.raises(ValidationError):
        GenerationResponse(proposal=None, unresolved_reason=None)


@pytest.mark.parametrize(
    "change", ["omitted_layer", "partial", "wrong_text", "no_evidence"]
)
def test_full_written_content_must_be_covered(change):
    data = sample_data().model_dump(mode="json")
    if change == "omitted_layer":
        data["content"]["l0_content"] = "unverified"
    elif change == "partial":
        data["claims"][0]["end"] -= 1
    elif change == "wrong_text":
        data["claims"][0]["conclusion"] = "different"
    else:
        data["claims"][0]["evidence"] = []
    with pytest.raises(ValidationError):
        ProposalData.model_validate(data)
