import json
import pytest
from uuid import UUID

from contexthub.discovery.adjudication import EvidenceSet
from contexthub.discovery.dependency_batch import BatchFact,batch_payload,parse_batch

BUNDLE=EvidenceSet(evidence=(),complete=True,at_max_scope=False,issues=())
IDS=(UUID(int=2),UUID(int=3))

def row(ident,**changes):
    j=dict(verdict='UNKNOWN',citation_ids=[],steps=[],negative_basis='none',
        exclusion_ids=[],conflicts=[],missing=['applicable rule'],semantic_basis='insufficient')
    j.update(changes)
    return dict(candidate_number=IDS.index(ident)+1 if ident in IDS else 99,judgment=j)


def test_one_bad_reference_does_not_discard_other_item():
    parsed=parse_batch(json.dumps({'items':[row(IDS[0],citation_ids=['E999999']),row(IDS[1])]}),IDS,BUNDLE)
    assert parsed.items[0].judgment is None
    assert parsed.items[0].errors
    assert parsed.items[1].judgment.missing==('applicable rule',)


def test_omission_is_not_negative_and_unknown_id_is_recorded():
    p=parse_batch(json.dumps({'items':[row(IDS[0]),row(UUID(int=99))]}),IDS,BUNDLE)
    assert p.items[1].errors==('missing_batch_item',)
    assert p.items[1].judgment is None
    assert p.errors==('undeclared_batch_candidate_id',)


def test_duplicate_not_silently_overwritten():
    p=parse_batch(json.dumps({'items':[row(IDS[0]),row(IDS[0]),row(IDS[1])]}),IDS,BUNDLE)
    assert p.items[0].errors==('duplicate_batch_item',)
    assert p.items[1].judgment is not None


def test_malformed_response_preserves_all_slots():
    p=parse_batch('not json',IDS,BUNDLE)
    assert len(p.items)==2 and all(i.judgment is None for i in p.items)
    assert p.errors==('invalid_batch_envelope',)


def test_dual_weak_payloads_share_evidence_not_answers():
    target=BatchFact(id=UUID(int=1),text='tax')
    cs=tuple(BatchFact(id=i,text=str(i)) for i in IDS)
    first=batch_payload(target,cs,BUNDLE,role='normal')
    second=batch_payload(target,cs,BUNDLE,role='challenge')
    assert first['evidence_sha256']==second['evidence_sha256']
    assert first['candidates']==list(reversed(second['candidates']))
    assert 'prior_judgments' not in first and 'prior_judgments' not in second


def test_entity_verdict_cannot_be_used_for_dependency():
    p=parse_batch(json.dumps({'items':[row(i,verdict='SAME') for i in IDS]}),IDS,BUNDLE)
    assert all(i.judgment is None and i.errors==('invalid_dependency_verdict',) for i in p.items)


def test_payload_hides_internal_uuids_and_uses_explicit_evidence_field():
    target=BatchFact(id=UUID(int=1),text='tax')
    cs=tuple(BatchFact(id=i,text='candidate') for i in IDS)
    p=batch_payload(target,cs,BUNDLE,role='normal')
    wire=json.dumps(p)
    assert all(str(i) not in wire for i in (target.id,*IDS))
    assert p['target']=={'text':'tax'}
    assert [c['candidate_number'] for c in p['candidates']]==[1,2]
    assert 'conclusion_evidence_id' in wire
    assert '"conclusion_id"' not in wire


def test_subset_mapping_retains_original_numbers_not_position():
    numbers={IDS[0]:1,IDS[1]:2}
    p=parse_batch(json.dumps({'items':[row(IDS[1])]}),(IDS[1],),BUNDLE,candidate_numbers=numbers)
    assert p.items[0].candidate_id==IDS[1] and not p.items[0].errors
    payload=batch_payload(BatchFact(id=UUID(int=1),text='target'),
        (BatchFact(id=IDS[1],text='candidate'),),BUNDLE,role='normal',candidate_numbers=numbers)
    assert payload['candidates'][0]['candidate_number']==2


def test_uuid_in_evidence_slot_is_not_reinterpreted_as_target():
    bad=row(IDS[0],steps=[dict(premise_ids=['E000001'],previous_steps=[],conclusion_evidence_id=str(UUID(int=1)))])
    p=parse_batch(json.dumps({'items':[bad,row(IDS[1])]}),IDS,BUNDLE)
    assert p.items[0].judgment is None and p.items[0].errors
    assert p.items[1].judgment is not None


def test_real_v18_failed_response_remains_failed_offline():
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]/'artifacts/patha-two-episode-20260926-v18/probes'
    archived=json.loads(next((root/'batch-responses').glob('*.json')).read_text())
    raw=json.loads(archived['response_text'])['choices'][0]['message']['content']
    result=parse_batch(raw,IDS,BUNDLE)
    assert result.errors and all(i.judgment is None for i in result.items)


@pytest.mark.parametrize('number',[True,'1',0,99])
def test_candidate_numbers_are_strict_positive_declared_integers(number):
    item=row(IDS[0]);item['candidate_number']=number
    p=parse_batch(json.dumps({'items':[item]}),IDS,BUNDLE)
    assert p.errors and all(i.judgment is None for i in p.items)
