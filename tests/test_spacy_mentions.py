"""Local trained-model integration checks; no model downloads or API calls."""
import json
import pytest
from contexthub.discovery.mention_ner import SpacyMentionConfig, entity_spans, check_ner
from contexthub.discovery.stage0 import Stage0Config, locate
from contexthub.models.knowledge import text_hash
from discovery_helpers import MemoryDB
from s4_helpers import evidence


@pytest.fixture
def ner():
    pytest.importorskip('en_core_web_sm')
    pytest.importorskip('zh_core_web_sm')
    return SpacyMentionConfig(spacy_version='3.8.14', english_model='en_core_web_sm',
                              english_version='3.8.0', chinese_model='zh_core_web_sm', chinese_version='3.8.0')


def config(ner):
    return Stage0Config(version='test-spacy', rules=(), aliases=(), alias_version='empty',
                        mention_patterns=(), mention_view='dialogue-content-v2', spacy_ner=ner)


@pytest.mark.parametrize('text,expected', [('Alice met Bob in London.', {'Alice','Bob','London'}),
                                         ('Good Morning, Alice!', {'Alice'}),
                                         ('张三今天去了北京。', {'张三','北京'})])
def test_trained_model_examples(ner, text, expected):
    found={text[a:b] for a,b in entity_spans(ner,text)}
    assert expected <= found
    assert 'Good Morning' not in found


@pytest.mark.asyncio
@pytest.mark.parametrize('ensure_ascii',[False,True])
async def test_real_locator_body_mapping_and_dedup(ner, ensure_ascii):
    raw=json.dumps([{'role':'Alice','content':'<p title="Bob">Alice met Bob in London.</p><p>张三今天去了北京。</p>'}],ensure_ascii=ensure_ascii)
    e=await evidence(MemoryDB(),raw)
    start=raw.index('London')
    overlap=e.model_copy(update={'start':start,'end':start+6,'excerpt':'London','excerpt_sha256':text_hash('London')})
    result=locate(config(ner),(e,overlap,e))
    decoded=[json.loads('"'+m.text+'"') for m in result]
    assert {'Alice','Bob','London','张三','北京'} <= set(decoded)
    assert decoded.count('Alice')==decoded.count('Bob')==1
    assert len({(m.evidence.source,m.start,len(m.text)) for m in result})==len(result)
    for m in result:assert raw[m.start:m.start+len(m.text)]==m.text


def test_no_regex_fallback_on_unavailable_or_wrong_version(ner):
    for update,code in [({'english_model':'missing_context_hub_ner_model'},'mention_ner_model_unavailable'),
                        ({'english_version':'0.0.0'},'mention_ner_version_mismatch')]:
        with pytest.raises(ValueError,match=code):check_ner(ner.model_copy(update=update))


def test_spacy_rejects_legacy_view_and_regex_union(ner):
    for update in [{'mention_view':'raw-v1'},{'mention_patterns':[r'(?P<mention>.*)']}]:
        with pytest.raises(ValueError,match='spacy_mentions_require_dialogue_without_regex'):
            Stage0Config.model_validate({**config(ner).model_dump(),**update})


@pytest.mark.xfail(strict=True, reason="Pinned Chinese model includes 一 in 张三一起; known semantic boundary failure, no repair")
def test_mixed_language_person_boundary(ner):
    text='Alice 和张三一起去了北京。'
    found={text[a:b] for a,b in entity_spans(ner,text)}
    assert 'Alice' in found and '张三' in found


def test_no_length_truncation(ner):
    from contexthub.discovery.mention_ner import _pipeline
    pipeline=_pipeline(ner.english_model,ner.english_version,ner.spacy_version)
    with pytest.raises(ValueError,match='mention_ner_input_too_long'):
        entity_spans(ner,'A'*(pipeline.max_length+1))


def test_mixed_language_preserves_both_models_predictions(ner):
    from contexthub.discovery.mention_ner import _recognize
    text = 'Alice 和张三一起去了北京。'
    expected = {(a, b) for model, version in [(ner.english_model, ner.english_version),
                                             (ner.chinese_model, ner.chinese_version)]
                for a, b, label in _recognize(model, version, ner.spacy_version, text)}
    assert set(entity_spans(ner, text)) == expected
