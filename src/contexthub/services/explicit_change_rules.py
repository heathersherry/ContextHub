"""Conservative, model-free rules for a small literal English grammar.

This module consumes versioned memory, never benchmark annotations. It cannot
resolve aliases/pronouns or manufacture a source value. Unsupported input returns
None and must use ordinary discovery/maintenance (or remain unresolved).
"""

import re
from dataclasses import dataclass

from contexthub.models.knowledge import canonical_hash
from contexthub.services.session_state import (
    Delta, Edge, Edit, SessionState, Verification, apply_delta, descendants, release_fresh,
)

GRAMMAR_VERSION = "literal-change-rule-v1"
_ATTRIBUTE = r"[A-Za-z][A-Za-z -]{0,79}?"
_RULE = re.compile(
    rf"If (?:my (?P<a>{_ATTRIBUTE}) changes|I change my (?P<b>{_ATTRIBUTE})), "
    rf"my (?P<target>{_ATTRIBUTE}) will (?:(?:switch|change) to|be) "
    r"(?P<value>[^\n\r.!?;]+)\.", re.IGNORECASE,
)
_FACT = re.compile(
    rf"My (?P<attribute>{_ATTRIBUTE}) is (?P<value>[^\n\r.!?;]+)\.",
    re.IGNORECASE,
)
# These constructs require semantic interpretation, not a substring match.
_QUALIFIER = re.compile(
    r"\b(?:if|unless|except|when|until|provided|assuming|and|or|but|not|never|"
    r"maybe|perhaps|probably|possibly|likely|might|may|could|should|would|"
    r"sometimes|usually|then|otherwise|this|that|these|those|it|they|he|she|"
    r"different|unknown|something|whatever|another|tbd)\b|\bno longer\b",
    re.IGNORECASE,
)


def _literal(value):
    return (bool(value.strip()) and value == value.strip()
            and not value.startswith("'") and not value.endswith("'")
            and not _QUALIFIER.search(value)
            and not any(c in value for c in '\"“”‘’`<>\\:='))


def _attribute(value):
    # Attribute case/spacing is grammar, values remain exact literal bytes.
    return " ".join(value.lower().split())


@dataclass(frozen=True)
class LiteralRule:
    upstream_attribute: str
    target_attribute: str
    replacement: str
    original: str


def parse_rule(text):
    """Full sentence only; never find an instruction inside a quote/example."""
    match = _RULE.fullmatch(text)
    if not match:
        return None
    upstream, target, value = match["a"] or match["b"], match["target"], match["value"]
    if not all(_literal(v) for v in (upstream, target, value)):
        return None
    if _attribute(upstream) == _attribute(target):
        return None
    return LiteralRule(_attribute(upstream), _attribute(target), value, text)


def parse_fact(text):
    match = _FACT.fullmatch(text)
    if not match or not all(_literal(match[k]) for k in ("attribute", "value")):
        return None
    return _attribute(match["attribute"]), match["value"]


@dataclass(frozen=True)
class RuleApplication:
    state: SessionState
    affected: tuple[str, ...]
    rule_id: str
    proof: Verification
    grammar_version: str = GRAMMAR_VERSION
    model_calls: int = 0


def explicit_edges(state, *, rule_id, upstream_id, target_id):
    """P1 fast path for exact, unique attributes in a single-owner memory.

    The rule node must be a grounded user rule already admitted by ingestion;
    callers must not turn an assistant quote or benchmark label into a rule.
    Return source->target AND rule->target: changing the rule matters too.
    """
    nodes = {n.id: n for n in state.nodes}
    if len(nodes) != len(state.nodes) or len({rule_id, upstream_id, target_id}) != 3:
        return None
    if not {rule_id, upstream_id, target_id} <= nodes.keys():
        return None
    if {rule_id, upstream_id} & descendants({target_id}, state.edges):
        return None
    rule_node, upstream, target = (nodes[i] for i in (rule_id, upstream_id, target_id))
    if (rule_node.kind != "rule" or rule_node.status != "fresh"
            or upstream.kind not in ("assertion", "derived")
            or target.kind not in ("assertion", "derived")
            or upstream.status == "deleted" or target.status == "deleted"
            or {rule_id, upstream_id, target_id} & set(state.pending_targets)):
        return None
    rule = parse_rule(rule_node.text)
    source_fact, target_fact = parse_fact(upstream.text), parse_fact(target.text)
    if (rule is None or source_fact is None or target_fact is None
            or source_fact[0] != rule.upstream_attribute
            or target_fact[0] != rule.target_attribute):
        return None
    # Duplicate assertions or overlapping conditional rules require resolution.
    for attribute in (rule.upstream_attribute, rule.target_attribute):
        matches = [n for n in state.nodes if n.status != "deleted"
                   and (fact := _FACT.fullmatch(n.text))
                   and _attribute(fact["attribute"]) == attribute]
        if len(matches) != 1:
            return None
    for node in state.nodes:
        if node.id == rule_id or node.status == "deleted":
            continue
        other = parse_rule(node.text) if node.kind == "rule" else None
        if ((other and other.target_attribute == rule.target_attribute)
                or (other is None and node.kind in ("rule", "uncertain") and
                    re.search(r"\b" + re.escape(rule.target_attribute) + r"\b", node.text, re.I))):
            return None
    return (
        Edge(upstream=upstream_id, downstream=target_id, status="confirmed",
             evidence=rule_node.evidence),
        Edge(upstream=rule_id, downstream=target_id, status="confirmed",
             evidence=rule_node.evidence),
    )


def apply_explicit_change(before, current, *, rule_id, upstream_id, target_id):
    """P2 exact replay + deterministic verification, with no model capability.

    `before` is the sealed pre-session state. `current` must already contain
    the grounded, fresh changed upstream and stale target. Unrelated incoming
    dependencies, missing/stale versions or uncertain identity force fallback.
    The caller persists this transition with the same basis CAS as other work.
    """
    edges = explicit_edges(current, rule_id=rule_id, upstream_id=upstream_id, target_id=target_id)
    if edges is None:
        return None
    old = {n.id: n for n in before.nodes}
    now = {n.id: n for n in current.nodes}
    if not {upstream_id, target_id, rule_id} <= old.keys():
        return None  # Rule learned with the change requires ordinary verification.
    source, target, rule_node = (now[i] for i in (upstream_id, target_id, rule_id))
    old_source, old_target = old[upstream_id], old[target_id]
    old_fact, new_fact = parse_fact(old_source.text), parse_fact(source.text)
    if (old_fact is None or new_fact is None or old_fact[0] != new_fact[0]
            or old_fact[1] == new_fact[1] or source.version <= old_source.version
            or old_source.status != "fresh" or source.status != "fresh"
            or target.status not in ("stale", "unresolved")
            or target.version != old_target.version or target.text != old_target.text
            or old[rule_id] != rule_node or rule_node.status != "fresh"):
        return None
    # A narrow literal rule cannot discharge unrelated causes/unknown edges.
    if any(e.downstream == target_id and
           (e.upstream not in (upstream_id, rule_id) or e.status != "confirmed")
           for e in current.edges):
        return None
    rule = parse_rule(rule_node.text)
    replacement = f"My {rule.target_attribute} is {rule.replacement}."
    if replacement == target.text:
        return None  # Do not manufacture a new version for unchanged content.
    evidence = tuple(dict.fromkeys((*rule_node.evidence, *source.evidence)))
    staged, affected = apply_delta(current, Delta(
        basis_sha256=canonical_hash(current),
        edits=(Edit(id=target_id, expected_version=target.version,
                    text=replacement, kind="derived", evidence=evidence),),
        searched=(target_id,), edges=edges, complete=True,
    ))
    nodes = {n.id: n for n in staged.nodes}
    proof = Verification(
        basis_sha256=canonical_hash(staged), target=target_id,
        target_version=nodes[target_id].version,
        upstream_versions=tuple(sorted((i, nodes[i].version) for i in (rule_id, upstream_id))),
        supported=True, applicable=True, conflicts_resolved=True, coverage_complete=True,
        evidence=evidence,
        receipt_id=canonical_hash(dict(grammar=GRAMMAR_VERSION, before=canonical_hash(before),
            current=canonical_hash(current), rule=rule_id, source=upstream_id, target=target_id)),
    )
    return RuleApplication(release_fresh(staged, proof), affected, rule_id, proof)


def maintain_explicit_changes(before, current):
    """Run the literal fast path before semantic maintenance, including chains.

    No retry/model calls; every success creates a version and a proof. Remaining
    stale/unresolved targets are returned explicitly for normal handling, never
    relabelled successful. Unsupported rules do not veto unrelated fast paths.
    """
    applications = []
    attempted = set()
    for _ in range(len(current.nodes)):
        progressed = False
        # Rules and bindings come only from the currently admitted memory.
        for rule_node in current.nodes:
            rule = parse_rule(rule_node.text) if rule_node.kind == "rule" else None
            if rule is None:
                continue
            sources = [n.id for n in current.nodes if n.status == "fresh"
                       and (f := parse_fact(n.text)) and f[0] == rule.upstream_attribute]
            targets = [n.id for n in current.nodes if n.status in ("stale", "unresolved")
                       and (f := parse_fact(n.text)) and f[0] == rule.target_attribute]
            if len(sources) != 1 or len(targets) != 1:
                continue
            key = (rule_node.id, sources[0], targets[0])
            if key in attempted:
                continue
            result = apply_explicit_change(before, current, rule_id=rule_node.id,
                                          upstream_id=sources[0], target_id=targets[0])
            if result is not None:
                attempted.add(key)
                applications.append(result)
                current, progressed = result.state, True
        if not progressed:
            break
    return current, tuple(applications), tuple(n.id for n in current.nodes
                                             if n.status in ("stale", "unresolved"))
