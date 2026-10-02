from uuid import UUID

import pytest
from contexthub.discovery.search import SearchCandidate
from contexthub.discovery.semantic_candidates import EmbeddedFact, FactIndexSnapshot, RetrievalPolicy, select_facts


def fixture():
    vectors = [(1.,0.),(.99,.01),(.9,.1),(.5,.5),(0.,1.),(-1.,0.)]
    facts = tuple(EmbeddedFact(candidate=SearchCandidate(id=UUID(int=i+1),
        kind='batch_proposition', proposition_id=UUID(int=i+1), sources=(), source_versions=(),
        text=f'fact {i}', evidence=(), explicit_provenance=()),vector=v) for i,v in enumerate(vectors))
    return FactIndexSnapshot(account_id='test',history_sha256='a'*64,
        embedding_identity_sha256='b'*64,facts=facts), RetrievalPolicy(initial_candidates=2,expansion_factor=2,batch_size=2,max_partial_rounds=3)


def test_required_outlier_not_lost_and_unexamined_not_negative():
    idx,p = fixture()
    r=select_facts(idx,p,UUID(int=1),required_ids=(UUID(int=6),))
    assert set(r.selected_ids)=={UUID(int=i) for i in (2,3,6)}
    assert set(r.unexamined_ids)=={UUID(int=i) for i in (4,5)}
    assert not r.universe_exhausted
    assert tuple(i for b in r.batches for i in b)==r.selected_ids


def test_gap_expansion_retains_picks_and_terminates():
    idx,p=fixture(); r=select_facts(idx,p,UUID(int=1)); seen=set(r.selected_ids)
    for _ in range(4):
        if r.universe_exhausted: break
        r=select_facts(idx,p,UUID(int=1),previous=r,gap_vector=(0.,1.))
        assert seen <= set(r.selected_ids)
        seen=set(r.selected_ids)
    assert r.universe_exhausted and len(seen)==5
    with pytest.raises(ValueError,match='retrieval_universe_exhausted'):
        select_facts(idx,p,UUID(int=1),previous=r)


def test_changed_snapshot_cannot_resume():
    idx,p=fixture();r=select_facts(idx,p,UUID(int=1))
    changed=idx.model_copy(update={'history_sha256':'c'*64})
    with pytest.raises(ValueError,match='cursor_mismatch'):
        select_facts(changed,p,UUID(int=1),previous=r)


def test_equal_scores_have_stable_order():
    idx,p=fixture()
    equal=idx.model_copy(update={'facts':tuple(f.model_copy(update={'vector':(1.,0.)}) for f in reversed(idx.facts))})
    assert select_facts(equal,p,UUID(int=1)).selected_ids==(UUID(int=2),UUID(int=3))


@pytest.mark.parametrize('vector',[(),(0.,0.),(float('nan'),1.),(float('inf'),1.)])
def test_invalid_embedding_rejected(vector):
    idx,_=fixture()
    with pytest.raises(ValueError): EmbeddedFact(candidate=idx.facts[0].candidate,vector=vector)


def test_bad_dimensions_and_duplicate_candidates_rejected():
    idx,_=fixture()
    for facts in [(*idx.facts,idx.facts[0]),(idx.facts[0],idx.facts[1].model_copy(update={'vector':(1.,)}))]:
        with pytest.raises(ValueError): FactIndexSnapshot(**{**idx.model_dump(), 'facts':facts})


def test_tampered_cursor_is_not_accepted_as_coverage():
    idx,p=fixture();r=select_facts(idx,p,UUID(int=1))
    with pytest.raises(ValueError,match='invalid_retrieval_cursor'):
        select_facts(idx,p,UUID(int=1),previous=r.model_copy(update={'unexamined_ids':()}))


def test_approved_three_partial_rounds_then_full_universe():
    idx,_=fixture()
    facts=tuple(EmbeddedFact(candidate=idx.facts[0].candidate.model_copy(update={
        'id':UUID(int=i+1),'proposition_id':UUID(int=i+1),'text':str(i)}),vector=(1.,float(i))) for i in range(101))
    idx=idx.model_copy(update={'facts':facts})
    p=RetrievalPolicy(initial_candidates=8,expansion_factor=2,batch_size=8,max_partial_rounds=3)
    r=select_facts(idx,p,UUID(int=1)); counts=[len(r.selected_ids)]
    for _ in range(3):
        r=select_facts(idx,p,UUID(int=1),previous=r);counts.append(len(r.selected_ids))
    assert counts==[8,16,32,100]
    assert r.universe_exhausted
