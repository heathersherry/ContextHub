import pytest

from scripts.memory_research.entity_groups import (
    EntityGroups, eligible_facts, lexemes, normalize_months, split_query_cutoff,
)


def row(key, entities=(), tokens=(), vector=(1., 0.), **changes):
    return {'memory_id': key, 'version': 1, 'tenant_id': 't', 'namespace': 'n', 'owner_id': 'o',
            'created_in_session': 1, 'temporal_scope': 'current', 'type': 'fact',
            'status': 'fresh', 'accepted': True, 'invalidated': False,
            'memory_embedding': list(vector), 'entities': list(entities), 'text': '',
            'search_vector': ' '.join(f"'{token}':{i}" for i, token in enumerate(tokens, 1)),
            **changes}


def test_latest_is_chosen_before_state_filter_and_scope_cutoff_are_preserved():
    base = row('a')
    rows = [base, row('a', version=2, status='deleted'), row('b', invalidated=True),
            row('c', namespace='other'), row('d', created_in_session=5),
            row('e', temporal_scope='historical'), row('f', accepted=False), row('g', type='rule'),
            row('h', memory_embedding=None), row('i'), row('i', version=2, created_in_session=6)]
    result = eligible_facts(rows, {'tenant_id': 't', 'namespace': 'n', 'owner_id': 'o'}, 4)
    assert list(result) == ['i:1']


def test_stored_lexemes_preserve_stemming_and_escaped_apostrophe():
    assert lexemes("'book':2 'don''t':5 'trip-ans':9,12") == {'book', "don't", 'trip-ans'}


def fixture():
    return {r['memory_id'] + ':1': r for r in [
        row('a', ['event'], ['month'], (1., 0.)),
        row('b', ['event', 'journey'], [], (1., 1.)),
        row('c', ['journey'], ['lodging'], (0., 1.)),
        row('d', ['unrelated'], ['noise'], (-1., -1.)),
    ]}


def test_one_expansion_recovers_bridge_between_two_seeds_and_keeps_whole_group():
    engine = EntityGroups(fixture(), [1., 1.], ['month', 'lodging'], neighbor_threshold=0.0)
    groups, _ = engine.groups(['a:1', 'c:1'])
    chain, = [g for g in groups if g['refs'] == ['a:1', 'b:1', 'c:1']]
    assert chain['connected']
    assert {tuple(e['entities']) for e in chain['links']} == {('event',), ('journey',)}
    selected = engine.select(['a:1', 'c:1'], budget=3)
    assert set(selected['selected_refs']) == {'a:1', 'b:1', 'c:1'}
    assert any(t['bridge_gain'] > 0 and t['best_path']['center'] == 'b:1' for t in selected['trace'])
    assert 'd:1' not in selected['candidate_refs']


@pytest.mark.parametrize('budget', [1, 2, 3])
def test_budget_dedup_and_preservation_of_each_selected_group(budget):
    engine = EntityGroups(fixture(), [1., 1.], ['month', 'lodging'], neighbor_threshold=0.0)
    selected = engine.select(['a:1', 'c:1'], budget=budget)
    refs = selected['selected_refs']
    assert len(refs) == len(set(refs)) <= budget
    assert set(refs) <= fixture().keys()
    for step in selected['trace']:
        assert set(step['refs']) <= set(refs)


def test_ablation_does_not_expand_and_qualified_neighbors_are_not_capped():
    rows = fixture()
    rows.update({f'x{i}:1': row(f'x{i}', ['event'], ['month']) for i in range(12)})
    engine = EntityGroups(rows, [1., 1.], ['month', 'lodging'], neighbor_threshold=0.0)
    baseline = engine.select(['a:1', 'c:1'], expand=False)
    assert set(baseline['selected_refs']) == {'a:1', 'c:1'}
    expanded = engine.select(['a:1', 'c:1'])
    assert expanded['neighborhoods']['a:1']['available'] == 13
    assert len(expanded['neighborhoods']['a:1']['kept']) == 13


def test_high_degree_entities_and_entity_heavy_facts_have_weaker_links():
    rows = {f'{i}:1': row(str(i), ['common']) for i in range(10)}
    rows.update({'a:1': row('a', ['rare', 'common']), 'b:1': row('b', ['rare']),
                 'c:1': row('c', ['rare', 'x', 'y', 'z'])})
    engine = EntityGroups(rows, [1., 1.], ['absent'], neighbor_threshold=0.0)
    assert engine.link('a:1', 'b:1')['quality'] > engine.link('a:1', '0:1')['quality']
    assert engine.link('a:1', 'b:1')['quality'] > engine.link('a:1', 'c:1')['quality']


def test_cutoff_is_separated_without_dropping_dates_in_question_content():
    query = 'As of 2028-06-09 10:00, which booking covers 2028-01-12?'
    assert split_query_cutoff(query) == ('2028-06-09 10:00', 'which booking covers 2028-01-12?')
    assert split_query_cutoff('Which booking covers 2028-01-12?') == (None, 'Which booking covers 2028-01-12?')


def test_written_and_numeric_months_share_features_without_using_cutoff_month():
    _, content = split_query_cutoff('As of 2028-06-09 10:00, which trip is in January?')
    assert normalize_months({'trip', 'januari'}, content) == {'trip', 'month:01'}
    assert 'month:01' in normalize_months({'trip'}, 'The trip runs 2028-01-12 through 2028-01-15.')
    assert 'month:06' not in normalize_months({'trip', 'januari'}, content)


def test_shared_entity_without_query_relevance_does_not_make_a_useful_addition():
    rows = {'a:1': row('a', ['shared'], ['topic'], (1., 0.)),
            'b:1': row('b', ['shared'], ['unrelated'], (0., 1.)),
            'c:1': row('c', ['shared'], ['detail'], (1., 1.))}
    engine = EntityGroups(rows, [1., 0.], ['topic', 'detail'], neighbor_threshold=0.0)
    groups, _ = engine.groups(['a:1'])
    irrelevant = engine.contribution(engine.score(('a:1', 'b:1')), {'a:1'}, groups, [], 0.5, 0)
    relevant = engine.contribution(engine.score(('a:1', 'c:1')), {'a:1'}, groups, [], 0.5, 0)
    assert irrelevant['coverage_gain'] == irrelevant['selection_score'] == 0
    assert relevant['selection_score'] > 0


def test_low_similarity_bridge_can_still_contribute_through_relevant_endpoints():
    rows = fixture()
    rows['b:1']['memory_embedding'] = [-1., 1.]
    engine = EntityGroups(rows, [1., 1.], ['month', 'lodging'], neighbor_threshold=0.0)
    groups, _ = engine.groups(['a:1', 'c:1'])
    paths = engine.bridge_paths(groups, ['a:1', 'c:1'])
    gain = engine.contribution(engine.score(('a:1', 'b:1', 'c:1')), {'a:1', 'c:1'}, groups, paths, 0.5, 0)
    assert gain['direct_relevance'][0]['query_cosine'] == 0
    assert gain['bridge_gain'] > 0


def test_existing_coverage_and_completed_bridge_cannot_be_credited_twice():
    engine = EntityGroups(fixture(), [1., 1.], ['month', 'lodging'], neighbor_threshold=0.0)
    groups, _ = engine.groups(['a:1', 'c:1'])
    paths = engine.bridge_paths(groups, ['a:1', 'c:1'])
    best_path = max(p['strength'] for p in paths)
    result = engine.contribution(engine.score(('c:1', 'd:1')), {'a:1', 'b:1', 'c:1'}, groups, paths, 1, best_path)
    assert result['coverage_gain'] == result['bridge_gain'] == result['selection_score'] == 0


def test_unrelated_or_noncomplementary_endpoints_get_no_bridge_credit():
    rows = fixture()
    rows['c:1']['memory_embedding'] = [-1., -1.]
    engine = EntityGroups(rows, [1., 1.], ['month', 'lodging'], neighbor_threshold=0.0)
    groups, _ = engine.groups(['a:1', 'c:1'])
    assert engine.bridge_paths(groups, ['a:1', 'c:1']) == []
    engine = EntityGroups(fixture(), [1., 1.], ['month'], neighbor_threshold=0.0)
    groups, _ = engine.groups(['a:1', 'c:1'])
    assert engine.bridge_paths(groups, ['a:1', 'c:1']) == []


def test_unconnected_facts_cannot_pool_query_coverage():
    engine = EntityGroups(fixture(), [1., 1.], ['month', 'lodging'], neighbor_threshold=0.0)
    groups, _ = engine.groups(['a:1', 'c:1'])
    paths = engine.bridge_paths(groups, ['a:1', 'c:1'])
    unconnected = engine.contribution(engine.score(('c:1',)), {'a:1'}, groups, paths, 0.5, 0)
    connected = engine.contribution(engine.score(('a:1', 'b:1', 'c:1')), {'a:1'}, groups, paths, 0.5, 0)
    assert unconnected['coverage_gain'] == 0
    assert connected['coverage_gain'] == 0.5


def test_neighbor_gate_uses_only_query_cosine_not_lexemes_or_entity_frequency():
    rows = {
        'seed:1': row('seed', ['shared'], ['topic'], (1., 0.)),
        'high:1': row('high', ['shared', 'x', 'y', 'z'], [], (1., 0.)),
        'low:1': row('low', ['shared'], ['topic', 'detail'], (0.6, 0.8)),
        'outside:1': row('outside', ['different'], ['topic'], (1., 0.)),
    }
    engine = EntityGroups(rows, [1., 0.], ['topic', 'detail'], neighbor_threshold=0.7)
    _, neighbors = engine.groups(['seed:1'])
    assert neighbors['seed:1']['kept'] == ['high:1']
    assert neighbors['seed:1']['scores'] == {'high:1': 1.0}


def test_similar_distinct_fact_is_not_penalized_after_related_fact_is_selected():
    rows = {
        'booked:1': row('booked', ['trip'], [], (1., 0.), text='The new hotel is booked.'),
        'cancelled:1': row('cancelled', ['trip'], [], (0.99, 0.1), text='The old hotel is cancelled.'),
        'other:1': row('other', ['elsewhere'], [], (0.6, 0.8), text='Another trip has a reservation.'),
    }
    engine = EntityGroups(rows, [1., 0.], [], neighbor_threshold=0.35)
    result = engine.select(['booked:1', 'other:1'], budget=2)
    assert result['selected_refs'] == ['booked:1', 'cancelled:1']


def test_threshold_is_inclusive_and_can_return_no_neighbors_without_losing_seeds():
    rows = {'a:1': row('a', ['shared'], [], (1., 0.)),
            'b:1': row('b', ['shared'], [], (0.6, 0.8))}
    boundary = EntityGroups(rows, [1., 0.], [], neighbor_threshold=0.6)
    assert boundary.groups(['a:1'])[1]['a:1']['kept'] == ['b:1']
    strict = EntityGroups(rows, [1., 0.], [], neighbor_threshold=0.7)
    result = strict.select(['a:1'])
    assert result['neighborhoods']['a:1']['kept'] == []
    assert result['selected_refs'] == ['a:1']


def test_negative_cosine_is_not_clamped_before_neighbor_thresholding():
    rows = {'a:1': row('a', ['shared']), 'b:1': row('b', ['shared'], [], (-1., 0.))}
    engine = EntityGroups(rows, [1., 0.], [], neighbor_threshold=0.0)
    assert engine.groups(['a:1'])[1]['a:1']['kept'] == []
