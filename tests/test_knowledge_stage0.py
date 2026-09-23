import pytest
from contexthub.discovery.stage0 import (
    Mention,
    PatternRule,
    Stage0Config,
    Alias,
    apply_stage0,
)
from discovery_helpers import MemoryDB
from s4_helpers import evidence


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,verdict",
    [
        ("explicit_alias", "SAME"),
        ("incompatible_types", "DIFFERENT"),
        ("different_identity", "DIFFERENT"),
    ],
)
async def test_original_pair_rules(kind, verdict):
    e = await evidence(MemoryDB(), "Alpha means Beta")
    l, r = Mention(text="Alpha", evidence=e), Mention(text="Beta", evidence=e)
    cfg = Stage0Config(
        version="fixture",
        alias_version="empty",
        aliases=(),
        mention_patterns=(),
        rules=(
            PatternRule(
                id="fixture-rule",
                version="1",
                kind=kind,
                pattern=r"(?P<left>Alpha) means (?P<right>Beta)",
            ),
        ),
    )
    out = apply_stage0(cfg, l, r, (e,))
    assert out.verdict == verdict and out.hits[0].evidence == (e,)
    assert out.provenance_retained


@pytest.mark.asyncio
async def test_alias_scope_ambiguity_and_conflict():
    e = await evidence(MemoryDB(), "company Alpha aka Beta")
    l, r = (
        Mention(text="Alpha", scope="company", evidence=e),
        Mention(text="Beta", scope="company", evidence=e),
    )
    cfg = Stage0Config(
        version="fixture",
        alias_version="table-3",
        mention_patterns=(),
        rules=(),
        aliases=(
            Alias(name="Alpha", scope="company", canonical_id="x"),
            Alias(name="Beta", scope="company", canonical_id="x"),
        ),
    )
    out = apply_stage0(cfg, l, r, (e,))
    assert out.verdict == "SAME" and out.hits[0].canonical_id == "x"
    assert (
        apply_stage0(cfg, l.model_copy(update={"scope": None}), r, (e,)).verdict
        == "UNRESOLVED"
    )
    cfg = cfg.model_copy(
        update={
            "rules": (
                PatternRule(
                    id="conflict",
                    version="1",
                    kind="different_identity",
                    pattern=r"(?P<left>Alpha) aka (?P<right>Beta)",
                ),
            )
        }
    )
    assert apply_stage0(cfg, l, r, (e,)).verdict == "UNRESOLVED"


@pytest.mark.asyncio
async def test_no_rule_no_ner_or_bare_word_inference():
    e = await evidence(MemoryDB(), "apple Apple")
    cfg = Stage0Config(
        version="fixture",
        alias_version="empty",
        aliases=(),
        rules=(),
        mention_patterns=(),
    )
    assert (
        apply_stage0(
            cfg,
            Mention(text="apple", evidence=e),
            Mention(text="Apple", evidence=e),
            (e,),
        ).verdict
        == "UNRESOLVED"
    )
