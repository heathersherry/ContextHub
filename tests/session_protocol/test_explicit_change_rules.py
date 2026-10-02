import pytest

from contexthub.services.explicit_change_rules import (
    parse_rule, explicit_edges, apply_explicit_change, maintain_explicit_changes,
)
from contexthub.services.session_state import (
    Node, Edge, SessionState, Delta, Edit, apply_delta, release_fresh, Verification,
)
from contexthub.models.knowledge import canonical_hash


def n(ident, text, kind="assertion"):
    return Node(id=ident, text=text, version=1, kind=kind, status="fresh", evidence=(ident + ":source-v1",))


def initial():
    return SessionState(nodes=(
        n("health", "My health condition is lactose intolerance."),
        n("medication", "My medication is Quelmithin."),
        n("rule", "If my health condition changes, my medication will switch to multivitamin.", "rule"),
        n("other", "My hobby is pottery."),
    ), edges=(Edge(upstream="health", downstream="medication", status="confirmed", evidence=("rule:source-v1",)),))


def changed(before):
    current, _ = apply_delta(before, Delta(basis_sha256=canonical_hash(before), edits=(
        Edit(id="health", expected_version=1, text="My health condition is high blood pressure.",
             kind="assertion", evidence=("health:source-v2",)),), searched=("health",), edges=(), complete=True))
    current = release_fresh(current, Verification(basis_sha256=canonical_hash(current), target="health",
        target_version=2, upstream_versions=(), supported=True, applicable=True, conflicts_resolved=True,
        coverage_complete=True, evidence=("health:source-v2",), receipt_id="grounded-direct-update"))
    return current


@pytest.mark.parametrize("text", [
    "If my health condition changes, my medication will switch to multivitamin.",
    "If I change my residence, my commute will be around 10 min.",
    "If my health condition changes, my exercise routine will be cycling weekends.",
])
def test_supported_literal_grammar(text):
    assert parse_rule(text) is not None


@pytest.mark.parametrize("text", [
    'He said: "If my health condition changes, my medication will be multivitamin."',
    "If my health condition changes, my medication will probably be multivitamin.",
    "If my health condition changes, my medication will be multivitamin unless my doctor objects.",
    "If my health condition changes and I move, my medication will be multivitamin.",
    "If my health condition changes, my medication will be A or B.",
    "If my health condition changes, my medication will not be multivitamin.",
    "If my health condition changes, this would likely change too.",
    "If my health condition changes, his medication will be multivitamin.",
    "If my health condition changes, my medication will be it.",
    "If my health condition changes, my medication will be multivitamin. Except on weekends.",
    "If my health condition changes, my medication will be 'multivitamin'.",
    "If my health condition changes, my health condition will be good.",
    "If my health condition changes, my medication will be something different.",
    "If my health condition changes, my medication will be no longer needed.",
])
def test_unsupported_never_guesses(text):
    assert parse_rule(text) is None


def test_p1_edges_include_rule_and_exact_subject_attribute():
    state = initial()
    edges = explicit_edges(state, rule_id="rule", upstream_id="health", target_id="medication")
    assert {(e.upstream, e.downstream) for e in edges} == {("health", "medication"), ("rule", "medication")}
    assert explicit_edges(state, rule_id="rule", upstream_id="other", target_id="medication") is None


def test_full_fast_path_no_model_and_current_version_proof():
    before = initial()
    result = apply_explicit_change(before, changed(before), rule_id="rule", upstream_id="health", target_id="medication")
    assert result.model_calls == 0
    nodes = {n.id: n for n in result.state.nodes}
    assert nodes["medication"].text == "My medication is multivitamin."
    assert nodes["medication"].version == 2
    assert nodes["medication"].status == "fresh"
    assert nodes["other"] == before.nodes[-1]
    assert result.proof.upstream_versions == (("health", 2), ("rule", 1))
    assert set(result.proof.evidence) == {"rule:source-v1", "health:source-v2"}


@pytest.mark.parametrize("fault", ["stale_source", "same_version", "same_value", "changed_rule", "extra_parent", "pending", "duplicate", "conflict", "deleted"])
def test_unsafe_conditions_fall_back(fault):
    before = initial()
    current = changed(before)
    nodes = {n.id: n for n in current.nodes}
    if fault == "stale_source": nodes["health"] = nodes["health"].model_copy(update={"status": "stale"})
    if fault == "same_version": nodes["health"] = nodes["health"].model_copy(update={"version": 1})
    if fault == "same_value": nodes["health"] = nodes["health"].model_copy(update={"text": before.nodes[0].text})
    if fault == "changed_rule": nodes["rule"] = nodes["rule"].model_copy(update={"version": 2})
    if fault == "deleted": nodes["health"] = nodes["health"].model_copy(update={"status": "deleted"})
    if fault == "duplicate": nodes["duplicate"] = n("duplicate", "My health condition is something else.")
    if fault == "conflict": nodes["conflict"] = n("conflict", "If my health condition changes, my medication might be something else.", "rule")
    current = current.model_copy(update={"nodes": tuple(nodes.values())})
    if fault == "pending": current = current.model_copy(update={"pending_targets": ("medication",)})
    if fault == "extra_parent": current = current.model_copy(update={"edges": (*current.edges,
        Edge(upstream="other", downstream="medication", status="confirmed", evidence=("unknown-rule",)))})
    assert apply_explicit_change(before, current, rule_id="rule", upstream_id="health", target_id="medication") is None


def test_chain_and_unresolved_preserved_without_whole_graph_replay():
    before = initial()
    before = before.model_copy(update={"nodes": (*before.nodes,
        n("delivery", "My delivery is monthly."),
        n("rule2", "If my medication changes, my delivery will be weekly.", "rule"),
        n("appointment", "My appointment is weekly tutoring.")),
        "edges": (*before.edges,
            Edge(upstream="medication", downstream="delivery", status="confirmed", evidence=("rule2:source-v1",)),
            Edge(upstream="health", downstream="appointment", status="confirmed", evidence=("dependency-only",)))})
    state, applications, unresolved = maintain_explicit_changes(before, changed(before))
    assert len(applications) == 2
    assert sum(a.model_calls for a in applications) == 0
    assert {n.id: n.text for n in state.nodes}["delivery"] == "My delivery is weekly."
    assert unresolved == ("appointment",)


def test_rule_from_same_session_does_not_get_retroactive_authority():
    before = initial()
    current = changed(before)
    before = before.model_copy(update={"nodes": tuple(n for n in before.nodes if n.id != "rule")})
    assert apply_explicit_change(before, current, rule_id="rule", upstream_id="health", target_id="medication") is None


def test_cycle_rejected_before_any_state_edit():
    state = initial()
    state = state.model_copy(update={"edges": (*state.edges,
        Edge(upstream="medication", downstream="health", status="pending", evidence=("uncertain-cycle",)))})
    assert explicit_edges(state, rule_id="rule", upstream_id="health", target_id="medication") is None
