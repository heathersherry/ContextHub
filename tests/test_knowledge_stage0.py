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


def dialogue_config(pattern=r'"(?P<mention>[^"\n]+)"'):
    return Stage0Config(version='dialogue-v2', alias_version='empty', aliases=(),
                        rules=(), mention_patterns=(pattern,), mention_view='dialogue-content-v2')


@pytest.mark.asyncio
async def test_dialogue_structure_html_and_overlaps_are_not_mentions():
    import json
    from contexthub.discovery.stage0 import locate
    from contexthub.models.knowledge import text_hash
    raw = json.dumps([{'role': 'user', 'content': '<div class="role">Meet "Alice" and "content".</div>'}])
    e = await evidence(MemoryDB(), raw)
    start = raw.index('Alice')
    part = e.model_copy(update=dict(start=start, end=start+5, excerpt='Alice', excerpt_sha256=text_hash('Alice'), provenance=('overlap',)))
    result = locate(dialogue_config(), (part, e, e))
    assert [m.text for m in result] == ['Alice', 'content']
    assert all(raw[m.start:m.start+len(m.text)] == m.text for m in result)
    assert all('overlap' in m.evidence.provenance for m in result)


@pytest.mark.asyncio
@pytest.mark.parametrize('ensure_ascii', [True, False])
async def test_json_escapes_and_non_latin_mentions_keep_exact_original(ensure_ascii):
    import json
    from contexthub.discovery.stage0 import locate
    raw = json.dumps([{'role': 'user', 'content': 'Names: "张三😀" and "A\\B".\n"Alice" meets "Alice".'}], ensure_ascii=ensure_ascii)
    e = await evidence(MemoryDB(), raw)
    result = locate(dialogue_config(), (e,))
    assert len(result) == 4
    assert result[2].start != result[3].start
    for m in result:
        assert raw[m.start:m.start+len(m.text)] == m.text
    assert json.loads('"'+result[0].text+'"') == '张三😀'


@pytest.mark.asyncio
async def test_malformed_or_partial_dialogue_fails_not_empty_success():
    from contexthub.discovery.stage0 import locate
    for raw in ['"role": "user"', '[{"role":"user","content":"x","content":"y"}]', 'plain text']:
        e = await evidence(MemoryDB(), raw)
        with pytest.raises(ValueError, match='invalid_complete_dialogue_for_mentions'):
            locate(dialogue_config(), (e,))


@pytest.mark.asyncio
async def test_legacy_contract_unchanged_and_html_hidden_text_excluded():
    import json
    from contexthub.discovery.stage0 import locate
    legacy = dialogue_config().model_copy(update={'mention_view': 'raw-v1'})
    assert 'mention_view' not in legacy.model_dump()
    e = await evidence(MemoryDB(), '"role"')
    assert locate(legacy, (e,))[0].text == 'role'
    raw = json.dumps([{'role': 'assistant', 'content': '<script>"Wrong"</script><style>"Wrong"</style><!-- "Wrong" --><p>"Right"</p>'}])
    e = await evidence(MemoryDB(), raw)
    assert [m.text for m in locate(dialogue_config(), (e,))] == ['Right']


@pytest.mark.asyncio
async def test_dialogue_offset_fragment_and_conflicting_overlap_rejected():
    import json
    from contexthub.discovery.stage0 import locate
    from contexthub.models.knowledge import text_hash
    raw = json.dumps([{'role': 'user', 'content': '"Alice"'}])
    e = await evidence(MemoryDB(), raw)
    shifted = e.model_copy(update={'start': 1, 'end': e.end + 1})
    with pytest.raises(ValueError, match='complete_dialogue_evidence_required'):
        locate(dialogue_config(), (shifted,))
    bad = e.model_copy(update={'start': 0, 'end': 1, 'excerpt': 'x', 'excerpt_sha256': text_hash('x')})
    with pytest.raises(ValueError, match='overlapping_mention_evidence_mismatch'):
        locate(dialogue_config(), (e, bad))


@pytest.mark.asyncio
async def test_body_patterns_find_unquoted_names_and_never_merge_occurrences():
    import json
    from contexthub.discovery.stage0 import locate
    cfg = dialogue_config(r'(?P<mention>Alice Smith|SEM)')
    raw = json.dumps([{'role': 'user', 'content': '<a title="SEM">Alice Smith</a> uses SEM. SEM differs here.'}])
    e = await evidence(MemoryDB(), raw)
    result = locate(cfg, (e,))
    assert [m.text for m in result] == ['Alice Smith', 'SEM', 'SEM']
    assert len({m.start for m in result}) == 3
