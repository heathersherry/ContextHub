from pathlib import Path
import pytest
from contexthub.models.knowledge import ContractError
from integrations.memebench.patha.artifacts import file_hash
from integrations.memebench.patha.run_gates import FileIdentity
from integrations.memebench.patha.official_adapter import (
    load_official_class,
    official_prompt,
    classify,
    OfficialJudgeRunner,
)
from examples.knowledge_fixture import configuration
from s8_helpers import scoring_label

OFFICIAL = Path(
    "/Users/sherrylin/Documents/PythonProjects/public/MEME-public/code/eval/judge.py"
)


def source():
    return FileIdentity(path=str(OFFICIAL), sha256=file_hash(OFFICIAL))


def test_actual_official_entity_values_abs_before_after_and_cas():
    cls = load_official_class(source())
    for kind in ("Cas", "Abs"):
        label = scoring_label(kind=kind)
        before = official_prompt(
            cls, sidecar=label, phase="before", question="Question", answer="Answer"
        )
        after = official_prompt(
            cls, sidecar=label, phase="after", question="Question", answer="Answer"
        )
        assert before == cls.BEFORE_COMMON_PROMPT.format(
            question="Question", gold="old value", agent_answer="Answer"
        )
        template = cls.CASCADE_D_PROMPT if kind == "Cas" else cls.CASCADE_U_PROMPT
        assert after == template.format(
            question="Question",
            gold=label.after_entity_values["target"],
            agent_answer="Answer",
        )
        assert "MUST NOT SUBSTITUTE THIS GOLD" not in after
    with pytest.raises(ContractError, match="scope"):
        official_prompt(cls, sidecar=label, phase="during", question="x", answer="x")


@pytest.mark.parametrize(
    "before,after,expected",
    [
        (True, True, "real"),
        (False, True, "trivial"),
        (True, False, "knew_but_failed"),
        (False, False, "never_knew"),
        (None, True, "unknown"),
    ],
)
def test_four_official_answer_classes(before, after, expected):
    assert classify(before, after) == expected


def test_official_code_hash_and_missing_authorization_zero_calls(monkeypatch):
    import integrations.memebench.patha.official_adapter as adapter

    calls = []

    def forbidden(*a, **k):
        calls.append(1)
        raise AssertionError("external client forbidden")

    monkeypatch.setattr(adapter, "OpenAIChatClient", forbidden)
    cfg = configuration().base.model_copy(update={"prompt_template": "{input_json}"})
    with pytest.raises(ContractError):
        OfficialJudgeRunner(
            cfg,
            official_source=source(),
            repository=None,
            account_id="judge",
            api_key="test",
            transport=None,
            mode="formal",
        )
    assert not calls
    with pytest.raises(ContractError, match="identity_mismatch"):
        load_official_class(source().model_copy(update={"sha256": "0" * 64}))
