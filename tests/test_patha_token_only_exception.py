import pytest
from decimal import Decimal
from contexthub.llm.request_parameters import RequestParameters
from contexthub.models.knowledge import ContractError
from contexthub.discovery.planner import Plan, choose_price, solve_mckp, Item
from contexthub.propagation.planner import PlannerConfig, choose_next
from integrations.memebench.patha.run_gates import RunManifest
from test_memebench_patha_run_gates import manifest_fixture


def test_unknown_seed_explicit_disabled_exception():
    p=RequestParameters(output_limit_field='max_tokens',reasoning_effort=None,response_format=None,temperature_supported=True,seed=dict(support='unknown',enabled=False))
    with pytest.raises(ContractError):p.require_resolved()
    p=p.model_copy(update={'disabled_unknown_seed_authorization':'user-approved'})
    p.require_resolved()
    assert 'seed' not in p.wire(500)


def test_p1_no_fabricated_cost_no_priced_or_budget_use():
    plans=tuple(Plan(id=str(i),q=i,c=None,optional_strong=bool(i),protocol='s4-token-only-zero-lambda-v1') for i in (0,1))
    assert choose_price(plans,0).optional_strong
    with pytest.raises(ContractError):choose_price(plans,1)
    with pytest.raises(ContractError):solve_mckp((Item(id='x',plans=plans),),100)
    with pytest.raises(ValueError):Plan(id='x',q=1,c=None,optional_strong=True)


def test_p2_only_legal_zero_lambda_escalates_without_amount():
    c=PlannerConfig(lambda_p2=0,lambda_unit='targets/currency',currency=None,cost_mode='token_only_zero_lambda',u_max_algorithm='single-target-count-v1',estimates=())
    assert choose_next('adaptive','M1',legal=True,stop_reason=None,config=c).action=='M2'
    assert choose_next('adaptive','M2',legal=True,stop_reason=None,config=c).action=='M3'
    assert choose_next('adaptive','M1',legal=False,stop_reason='verifier_failed',config=c).action is None
    with pytest.raises(ValueError):PlannerConfig.model_validate(c.model_dump()|{'lambda_p2':1})
    with pytest.raises(ValueError):PlannerConfig.model_validate(c.model_dump()|{'cost_mode':'priced'})


def test_exception_never_formal_or_extra_episodes_repeats_grid(tmp_path):
    m,_=manifest_fixture(tmp_path)
    d=m.model_dump(mode='json'); ids=['ep_8185eda1823fe081718f7d90','ep_a6132508d3bb0eeae1c5b899']; d['episode_ids']=ids;d['development']['episode_ids']=ids;d['two_episode_token_only_authorization']='user-approved'
    RunManifest.model_validate(d)
    for change in [{'mode':'formal'},{'episode_ids':ids[:1]},{'statistics':d['statistics']|{'repeats':2}},{'development':d['development']|{'lambda_p1_grid':[0,1]}}]:
        with pytest.raises(ValueError):RunManifest.model_validate(d|change)
