"""Whole-session incremental maintenance on the versioned session store.

The legacy fixed-graph S8 service remains unchanged. This explicit protocol uses
the same recorded clients/ledger and planner, with checkpoint-scoped graphs and
proofs instead of attempting to reuse a watch bound to an obsolete graph version.
"""
import asyncio
import json
from math import fsum, sqrt, isfinite
from uuid import uuid5
from copy import deepcopy

from contexthub.models.knowledge import ContractError, canonical_hash
from contexthub.propagation.planner import choose_next
from .session_state import Delta, Edit, Edge, Verification, apply_delta, release_fresh
from .session_memory import SessionInput, Checkpoint, Vector, bind_span, extraction_evidence_view, answer_view
from .session_models import Extraction, Dependencies, IdentityAnswer, Repair, Verdict, IngestVerdict, PROMPTS
from .session_retrieval import terms, overlap, source_search
from .explicit_change_rules import parse_rule, explicit_edges, maintain_explicit_changes


class SessionKnowledgeService:
    def __init__(self, config, store, models):
        self.config, self.store, self.models = config, store, models
        self.checkpoint = None
        self.state = None
        self.evidence = {}
        self.audits = []
        self.history = []
        self.vectors = {}
        self.read_ids = {}
        self.attempts = {}
        self.source_watches = {}
        self.gaps = {}
        self._busy = False
        self.failure = None

    async def _ask(self, role, operation, payload, schema, *, stage):
        wire = dict(instruction=PROMPTS[operation], input=payload, answer_schema=schema.model_json_schema())
        sources = payload.get("sources", [payload["session"]] if "session" in payload else ())
        self.audits.append(dict(operation="request", role=role, model_operation=operation,
            stage="P1-reference" if getattr(self.models, "reference", False) else stage,
            arm=self.config.search, input_sha256=canonical_hash(wire),
            input_characters=len(json.dumps(wire, ensure_ascii=False)),
            raw_sources=[(r["source_id"], r["source_version"], r["content_sha256"]) for r in sources],
            raw_message_count=sum(len(r["messages"]) for r in sources),
            memory_count=len(payload.get("candidates", payload.get("memory", ()))),
            evidence_count=len(payload.get("evidence", ()))))
        basis = canonical_hash(self.state)
        result = None
        try:
            result = await self.models.ask(role, operation, payload, schema, stage=stage)
        finally:
            raw = getattr(self.models, "last_response", None)
            if result is not None or raw is not None:
                self.audits.append(dict(operation="model_response", role=role, model_operation=operation,
                    response=result.model_dump(mode="json") if result is not None else None,
                    raw_response=deepcopy(raw), call_ids=[str(i) for i in self.models.call_ids]))
        if basis != canonical_hash(self.state):
            raise ContractError("session_model_basis_drift")
        return result

    async def assert_empty(self):
        await self.store.assert_empty()

    async def snapshot(self):
        checkpoint = await self.store.load()
        if checkpoint is None:
            return {}
        if checkpoint.status not in ("complete", "unresolved"):
            raise ContractError("session_not_drained")
        return checkpoint.model_dump(mode="json")

    async def answer_snapshot(self):
        checkpoint = await self.store.load()
        return answer_view(checkpoint)

    def _nodes(self):
        return {n.id: n for n in self.state.nodes}

    def _remember(self, node):
        # Preserve the validity at the preceding sealed session, not the stale
        # status assigned while this session invalidates the old graph.
        previous = next((n for n in self.before.nodes if (n.id, n.version) == (node.id, node.version)), node)
        match = next((i for i, n in enumerate(self.history) if (n.id, n.version) == (node.id, node.version)), None)
        if match is None:
            self.history.append(previous)
        elif previous.status == "fresh":
            self.history[match] = previous

    def _apply(self, edits=(), searched=(), edges=(), complete=True, invalidated=()):
        nodes = self._nodes()
        for edit in edits:
            if edit.id in nodes:
                self._remember(nodes[edit.id])
        delta = Delta(basis_sha256=canonical_hash(self.state), edits=tuple(edits),
                      searched=tuple(searched), edges=tuple(edges), complete=complete, invalidated=tuple(invalidated))
        self.state, affected = apply_delta(self.state, delta)
        self.audits.append(dict(operation="delta", delta=delta.model_dump(mode="json"),
                                affected=affected, result_sha256=canonical_hash(self.state)))
        return affected

    def _proof(self, target, verdict, *, evidence_ids=None, verification_scope="raw-stage-package"):
        nodes = self._nodes()
        parents = {e.upstream for e in self.state.edges if e.downstream == target}
        if not parents and (nodes[target].kind == "derived" or not verdict.independent):
            return False
        evidence = tuple(dict.fromkeys((*nodes[target].evidence,
            *(verdict.evidence_ids if evidence_ids is None else evidence_ids))))
        if not evidence or not set(evidence) <= self.evidence.keys():
            raise ContractError("session_proof_evidence_missing")
        proof = Verification(basis_sha256=canonical_hash(self.state), target=target,
            target_version=nodes[target].version,
            upstream_versions=tuple(sorted((p, nodes[p].version) for p in parents)),
            # The common release receipt's support gate means candidate consistency
            # for ingestion; raw support remains unchanged for P2 Verdict.
            supported=(self._ingest_passed(verdict) if isinstance(verdict, IngestVerdict)
                       else verdict.supported and verdict.kind_confirmed),
            applicable=verdict.applicable, conflicts_resolved=verdict.conflicts_resolved,
            coverage_complete=target not in self.state.pending_targets,
            evidence=evidence, receipt_id="p_" + canonical_hash(dict(
                state=canonical_hash(self.state), verdict=verdict.model_dump(mode="json"), target=target)))
        self.state = release_fresh(self.state, proof)
        self.audits.append(dict(operation="release", proof=proof.model_dump(mode="json"),
                               verification_scope=verification_scope))
        self.source_watches[target] = evidence
        if not self._literal_user_rule(nodes[target]):
            self._stop(target, "verified")

    def _related(self, target):
        nodes = self._nodes()
        ids = {target} | {e.upstream for e in self.state.edges if e.downstream == target}
        ids |= set(self.read_ids.get(target, ()))
        text = nodes[target].text
        # Rules/conflicts and lexical entity/attribute candidates are not top-k capped.
        ids |= {n.id for n in nodes.values() if n.status != "deleted" and overlap(text, n.text)}
        return ids & nodes.keys()

    @staticmethod
    def _memory_fact(node):
        # Allowlist, not removal of today's known raw fields. Provenance stays local.
        return node.model_dump(mode="json", include={
            "id", "version", "basis_revision", "text", "kind", "status"})

    async def _verify_ingest(self, node, *, previous=None, action="ingest", candidate_read_ids=None):
        nodes = self._nodes()
        reads = tuple(self.read_ids.get(node.id, ()) if candidate_read_ids is None else candidate_read_ids)
        required = self._related(node.id)
        required.update(reads)
        required |= {n.id for n in nodes.values() if n.status != "deleted" and overlap(node.text, n.text)}
        # Newly staged memories are indexed before this path. A deletion proposal
        # uses the existing target's vector plus lexical matches of the request.
        ranked = self._rank(node.id) if node.id in self.vectors else []
        selected = required | set(ranked[:self.config.initial_candidates])
        selected.discard(node.id)
        required.discard(node.id)
        facts = {i: nodes[i] for i in selected}
        visible = {node.id, *facts}
        candidate = self._memory_fact(node)
        candidate.pop("status")  # A proposal, not a pre-existing unresolved premise.
        payload = dict(target=candidate, target_role="current_ingestion_candidate", action=action,
            previous=self._memory_fact(previous) if previous else None,
            memory=[self._memory_fact(facts[i]) for i in sorted(facts)],
            known_upstream_ids=sorted(e.upstream for e in self.state.edges if e.downstream == node.id),
            candidate_read_memory_ids=list(reads),
            allowed_memory_ids=sorted(visible), allowed_evidence_ids=[],
            graph=[e.model_dump(mode="json", include={"upstream", "downstream", "status", "judgment"})
                   for e in self.state.edges if e.upstream in visible and e.downstream in visible])
        verdict = await self._ask("verifier", "verify_ingest", payload, IngestVerdict, stage="P1")
        if (not set(verdict.memory_ids) <= visible or node.id not in verdict.memory_ids
                or not set(verdict.dependency_ids) <= set(verdict.memory_ids) - {node.id}
                or (verdict.independent and verdict.dependency_ids)):
            raise ContractError("session_ingest_memory_references_invalid")
        # This is program provenance, not raw evidence the verifier has read.
        self.audits.append(dict(operation="ingest_verifier", target=node.id,
            verification_scope="candidate-consistency-v2", raw_fidelity_verified=False,
            input_sha256=canonical_hash(payload), selected_ids=sorted(facts), required_ids=sorted(required),
            inventory_count=len(nodes), verdict=verdict.model_dump(mode="json"),
            provenance={i: dict(version=n.version, basis_revision=n.basis_revision,
                                evidence_ids=list(n.evidence))
                        for i in verdict.memory_ids for n in [node if i == node.id else facts[i]]}))
        return verdict

    @staticmethod
    def _ingest_passed(verdict):
        return (verdict.applicable and verdict.prerequisites_satisfied and verdict.conflicts_resolved
                and verdict.kind_confirmed and verdict.complete and not verdict.missing)

    def _payload(self, target, evidence_ids, *, full=False, memory_ids=None):
        ids = set(evidence_ids)
        if not ids <= self.evidence.keys():
            raise ContractError("session_unknown_evidence")
        selected = self.checkpoint.sources if full else tuple(s for s in self.checkpoint.sources
            if any(self.evidence[i].source_id == s.source_id for i in ids))
        allowed_sources = {s.source_id for s in selected}
        for source in selected:
            for index, message in enumerate(source.messages):
                if message.content:
                    bound = bind_span(source, index, 0, len(message.content))
                    self.evidence[bound.id] = bound
        visible = set(self._nodes()) if full else set(memory_ids or ())
        return dict(target=target, sources=[s.model_dump(mode="json") for s in selected],
                    evidence=[e.model_dump(mode="json") for e in self.evidence.values()
                              if e.source_id in allowed_sources],
                    memory=[n.model_dump(mode="json") for n in self.state.nodes if n.id in visible],
                    graph=[e.model_dump(mode="json") for e in self.state.edges
                           if e.upstream in visible and e.downstream in visible])

    async def _verify(self, target, evidence, *, extra=None, stage="P2", package=None):
        if package is None:
            ident = target.get("id")
            related = self._related(ident) if ident in self._nodes() else set()
            refs = set(evidence) | {e for i in related for e in self._nodes()[i].evidence}
            refs.update(ref for edge in self.state.edges if edge.downstream == ident for ref in edge.evidence)
            if extra and extra.get("previous"):
                refs.update(extra["previous"].get("evidence", ()))
            payload = self._payload(target, refs, memory_ids=related)
        else:
            payload = deepcopy(package)
            payload["target"] = target
        if extra:
            payload.update(extra)
        allowed = {e["id"] for e in payload["evidence"]}
        basis = canonical_hash(self.state)
        verdict = await self._ask("verifier", "verify", payload, Verdict, stage=stage)
        if basis != canonical_hash(self.state):
            raise ContractError("session_verification_basis_drift")
        if not verdict.evidence_ids or not set(verdict.evidence_ids) <= allowed:
            raise ContractError("session_verifier_evidence_invalid")
        self.audits.append(dict(operation="verifier", input_sha256=canonical_hash(payload),
                               evidence_package=canonical_hash({k: payload[k] for k in ("sources", "evidence", "memory", "graph")}),
                               sent_sources=[r["source_id"] for r in payload["sources"]],
                               verdict=verdict.model_dump(mode="json")))
        return verdict

    def _condition(self, target):
        nodes = self._nodes()
        parents = {e.upstream for e in self.state.edges if e.downstream == target}
        # Target status is an outcome; upstream status is a meaningful new condition.
        node = nodes[target].model_dump(mode="json", exclude={"status"})
        return canonical_hash(dict(account=self.store.account_id, stream=str(self.store.stream_id),
            config=self.config.fingerprint(), target=node,
            parents=[nodes[i].model_dump(mode="json") for i in sorted(parents)],
            edges=[e.model_dump(mode="json") for e in self.state.edges if e.downstream == target],
            candidate_reads=[nodes[i].model_dump(mode="json") for i in sorted(self.read_ids.get(target, ())) if i in nodes],
            pending=target in self.state.pending_targets,
            watches=self.source_watches.get(target, ()), gaps=self.gaps.get(target, ()),
            trigger=self.attempts.get(target, {}).get("trigger")))

    def _stop(self, target, reason, actions=()):
        old = self.attempts.get(target, {})
        clues = self.gaps.get(target, ()) or (self._nodes()[target].text,)
        clues = (*clues, *(self.evidence[e].quote for e in self.source_watches.get(target, ())))
        seen = set(old.get("seen_material", ()))
        seen.update(canonical_hash(m.content) for source in self.checkpoint.sources for m in source.messages
                    if any(overlap(clue, m.content) for clue in clues))
        old["seen_material"] = sorted(seen)
        self.attempts[target] = dict(old, condition=self._condition(target), reason=reason,
            actions=list(actions), last_ordinal=self.models.ordinal, read_sources=sorted(set(old.get("read_sources", ()))),
            clues=list(self.gaps.get(target, ())))
        self.audits.append(dict(operation="stop", target=target, **self.attempts[target]))

    def _already_stopped(self, target):
        return self.attempts.get(target, {}).get("condition") == self._condition(target)

    def _wake(self, source, edits):
        changed = {e.id for e in edits}
        targets = set()
        for target, record in self.attempts.items():
            if record.get("last_ordinal", -1) >= source.ordinal:
                continue
            if target not in self._nodes() or self._nodes()[target].status == "deleted":
                continue
            clues = self.gaps.get(target, ()) or (self._nodes()[target].text,)
            clues = (*clues, *(self.evidence[e].quote for e in self.source_watches.get(target, ())))
            # A repeated clue without a new value does not reset the action sequence.
            matched = sorted({m.content for m in source.messages
                              if any(overlap(clue, m.content) for clue in clues)})
            seen = set(record.get("seen_material", ()))
            novel = [t for t in matched if canonical_hash(t) not in seen]
            if novel:
                seen.update(canonical_hash(t) for t in novel)
                record["seen_material"] = sorted(seen)
                record["trigger"] = canonical_hash(sorted(seen))
                record["trigger_sources"] = [source.source_id]
                targets.add(target)
                self.audits.append(dict(operation="wake", target=target, source=source.source_id,
                                        reason="new_lexical_gap_or_basis_material"))
            elif record.get("reason") == "search_incomplete" and edits:
                # Conservative scope review only after a declared batch of new
                # material, never merely a new session/prefix or graph revision.
                material = {canonical_hash(e.text) for e in edits}
                consumed = set(record.get("scope_seen", ()))
                pending = set(record.get("scope_new", ())) | (material - consumed)
                record["scope_seen"] = sorted(consumed | material)
                if len(pending) >= self.config.scope_recheck_new_materials:
                    record["trigger"] = canonical_hash(sorted(pending))
                    record["scope_new"] = []
                    targets.add(target)
                    self.audits.append(dict(operation="wake", target=target,
                        reason="bounded_incomplete_scope_review", new_material_count=len(pending)))
                else:
                    record["scope_new"] = sorted(pending)
                if not self._already_stopped(target):
                    targets.add(target)
            elif target in changed or not self._already_stopped(target):
                targets.add(target)
                self.audits.append(dict(operation="wake", target=target, reason="related_version_or_validity"))
        return targets

    @staticmethod
    def _passed(v):
        return v.applicable and v.supported and v.conflicts_resolved and v.kind_confirmed

    def _literal_user_rule(self, node):
        if node.kind != "rule" or parse_rule(node.text) is None:
            return False
        return any(e.quote == node.text and e.role == "user"
                   and self.checkpoint.sources[next(i for i, s in enumerate(self.checkpoint.sources)
                       if s.source_id == e.source_id)].messages[e.message].content == node.text
                   for ident in node.evidence if (e := self.evidence[ident]))

    async def _index(self):
        active = [n for n in self.state.nodes if n.status != "deleted"]
        binding = lambda n: canonical_hash(dict(account=self.store.account_id,
            config=canonical_hash(self.config.embedding), id=n.id, version=n.version,
            text=n.text, evidence=[self.evidence[e].model_dump(mode="json") for e in n.evidence]))
        missing = [n for n in active if n.id not in self.vectors or self.vectors[n.id].binding != binding(n)]
        vectors = await self.models.embed([n.text for n in missing]) if missing else ()
        if len(vectors) != len(missing):
            raise ContractError("session_embedding_count_mismatch")
        for node, vector in zip(missing, vectors):
            if (len(vector) != self.config.embedding.dimensions or not all(isfinite(x) for x in vector)
                    or not isfinite(fsum(x*x for x in vector)) or fsum(x*x for x in vector) <= 0):
                raise ContractError("session_embedding_invalid")
            self.vectors[node.id] = Vector(node_id=node.id, binding=binding(node), values=vector)
        self.vectors = {n.id: self.vectors[n.id] for n in active}
        self.audits.append(dict(operation="index", embedded_ids=[n.id for n in missing],
                               reused=len(active)-len(missing)))

    def _rank(self, target):
        query = self.vectors[target].values
        norm = sqrt(fsum(x*x for x in query))
        def score(ident):
            values = self.vectors[ident].values
            other = sqrt(fsum(x*x for x in values))
            return fsum((a/norm)*(b/other) for a, b in zip(query, values))
        return sorted((i for i in self.vectors if i != target),
                      key=lambda i: (-overlap(self._nodes()[target].text, self._nodes()[i].text), -score(i), i))

    async def _dependencies(self, target):
        nodes = self._nodes()
        # Narrow exact rule bindings bypass only the proven edges, never suppress
        # dependencies of a current fact merely because its text contains 'if'.
        literal = []
        if self.config.explicit_rules:
            for rule in nodes.values():
                if rule.kind != "rule" or not self._literal_user_rule(rule):
                    continue
                for upstream in nodes.values():
                    bound = explicit_edges(self.state,
                        rule_id=rule.id, upstream_id=upstream.id, target_id=target)
                    if bound:
                        literal.extend(bound)
        if literal and not (set(self.read_ids.get(target, ())) - {e.upstream for e in literal} - {target}):
            existing = [e for e in self.state.edges if e.downstream == target]
            merged = {(e.upstream, e.downstream): e for e in (*existing, *literal)}
            # Do not erase other possible prerequisites with a literal match.
            self._apply(searched=(target,), edges=tuple(merged.values()))
            self.audits.append(dict(operation="literal_dependencies", target=target))
            return
        ranked = self._rank(target)
        required = self._related(target) - {target}
        # Deleted old parents remain visible as historical, unusable support.
        ranked = list(dict.fromkeys((*ranked, *sorted(required))))
        limit = len(ranked) if self.config.search == "full-history" else self.config.initial_candidates
        raw_sources, raw_rounds = set(), 0
        response = None
        for generation in range(self.config.max_partial_rounds + self.config.max_raw_rounds + 2):
            if generation >= self.config.max_partial_rounds:
                limit = len(ranked)
            selected = set(ranked[:limit]) | required
            def visible_memory(node):
                fact = self._memory_fact(node)
                if raw_sources:
                    fact["evidence"] = [e for e in node.evidence if self.evidence[e].source_id in raw_sources]
                return fact
            payload = dict(target=visible_memory(nodes[target]),
                candidates=[visible_memory(nodes[i]) for i in sorted(selected)],
                allowed_memory_ids=sorted({target, *selected}),
                allowed_evidence_ids=sorted(e.id for e in self.evidence.values() if e.source_id in raw_sources),
                sources=[s.model_dump(mode="json") for s in self.checkpoint.sources if s.source_id in raw_sources],
                evidence=[e.model_dump(mode="json") for e in self.evidence.values() if e.source_id in raw_sources],
                scope_exhausted=selected == set(ranked),
                search_clues=list(self.gaps.get(target, ())),
                new_source_locators=sorted(raw_sources & set(self.attempts.get(target, {}).get("trigger_sources", ()))))
            response = await self._ask("p1_weak", "dependency", payload, Dependencies, stage="P1")
            ids = [p.upstream_id for p in response.positives]
            if len(ids) != len(set(ids)) or not set(ids) <= selected:
                raise ContractError("session_dependency_candidates_invalid")
            allowed = {e["id"] for e in payload["evidence"]}
            # Explicit, executable raw requests. Whole sessions preserve role/time,
            # negation and cross-turn references; query can find unextracted facts.
            requested = set()
            for request in response.raw_requests:
                if not set(request.memory_ids) <= {target, *selected}:
                    raise ContractError("session_raw_memory_outside_candidates")
                if not set(request.source_ids) <= {s.source_id for s in self.checkpoint.sources}:
                    raise ContractError("session_raw_request_outside_prefix")
                if not set(request.source_ids) <= raw_sources:
                    raise ContractError("session_raw_source_not_supplied")
                requested.update(request.source_ids)
                requested.update(self.evidence[e].source_id for i in request.memory_ids for e in nodes[i].evidence)
                if request.query:
                    requested.update(s.source_id for s in source_search(self.checkpoint.sources, request.query))
                if not (request.memory_ids or request.source_ids or request.query):
                    raise ContractError("session_raw_request_without_locator")
            def check_positive_references(proposals):
                for proposal in proposals:
                    if (not set(proposal.memory_ids) <= {target, *selected}
                            or not set(proposal.evidence_ids) <= allowed):
                        raise ContractError("session_dependency_references_invalid")
            check_positive_references(response.positives)
            for q in response.identity_questions:
                if (q.left_evidence not in allowed or q.right_evidence not in allowed
                        or q.candidate_id not in ids or q.left_evidence not in nodes[q.candidate_id].evidence
                        or q.right_evidence not in nodes[target].evidence):
                    raise ContractError("session_identity_binding_invalid")
                requested.update(self.evidence[e].source_id for e in (q.left_evidence, q.right_evidence))
            if not requested <= {s.source_id for s in self.checkpoint.sources}:
                raise ContractError("session_raw_request_outside_prefix")
            new_sources = requested - raw_sources
            if new_sources and raw_rounds < self.config.max_raw_rounds:
                if len(raw_sources | new_sources) > self.config.raw_source_limit:
                    self.audits.append(dict(operation="raw_limit", target=target,
                        remaining_sources=sorted(new_sources), reason="source_limit_exceeded"))
                    break
                raw_sources.update(new_sources)
                raw_rounds += 1
                for source in self.checkpoint.sources:
                    if source.source_id in new_sources:
                        for index, message in enumerate(source.messages):
                            if message.content:
                                bound = bind_span(source, index, 0, len(message.content))
                                self.evidence[bound.id] = bound
                self.audits.append(dict(operation="raw_lookup", target=target, requests=[r.model_dump(mode="json") for r in response.raw_requests],
                    sources=sorted(new_sources), round=raw_rounds))
                continue
            identity_results = []
            identity_unknown = False
            for q in response.identity_questions:
                if q.left_evidence not in allowed or q.right_evidence not in allowed:
                    identity_unknown = True
                    continue
                if q.left not in self.evidence[q.left_evidence].quote or q.right not in self.evidence[q.right_evidence].quote:
                    raise ContractError("session_identity_mention_missing")
                ip = dict(question=q.model_dump(mode="json"), evidence=payload["evidence"], sources=payload["sources"])
                answer = await self._ask("p1_weak", "identity", ip, IdentityAnswer, stage="P1")
                if answer.verdict == "UNKNOWN":
                    answer = await self._ask("p1_strong", "identity", ip, IdentityAnswer, stage="P1")
                if not answer.evidence_ids or not set(answer.evidence_ids) <= allowed:
                    raise ContractError("session_identity_evidence_invalid")
                identity_unknown |= answer.verdict != "SAME"
                identity_results.append(dict(candidate_id=q.candidate_id, **answer.model_dump(mode="json")))
            def valid_positive(p):
                return (p.upstream_id in p.memory_ids and set(p.memory_ids) <= {target, *selected}
                        and set(p.evidence_ids) <= allowed and not p.review)
            flagged = [p for p in response.positives if not valid_positive(p)]
            if flagged and not identity_unknown:
                review = dict(payload, prior_proposals=[p.model_dump(mode="json") for p in flagged], identity_results=identity_results)
                reviewed = await self._ask("p1_strong", "dependency", review, Dependencies, stage="P1")
                check_positive_references(reviewed.positives)
                flagged_ids = {p.upstream_id for p in flagged}
                if (any(p.upstream_id not in flagged_ids for p in reviewed.positives) or reviewed.identity_questions
                        or len({p.upstream_id for p in reviewed.positives}) != len(reviewed.positives)):
                    raise ContractError("session_review_outside_proposals")
                response = response.model_copy(update={
                    "positives": tuple(p for p in response.positives if p.upstream_id not in flagged_ids) + reviewed.positives,
                    "complete": response.complete and reviewed.complete and not reviewed.raw_requests,
                    "missing": reviewed.missing or response.missing})
            complete = (response.complete and not response.missing and not response.raw_requests
                        and not new_sources and not identity_unknown and all(valid_positive(p) for p in response.positives)
                        and (bool(response.positives) or response.independent))
            edges = tuple(Edge(upstream=p.upstream_id, downstream=target,
                status="confirmed" if complete else "pending",
                evidence=p.evidence_ids or tuple(dict.fromkeys(e for i in p.memory_ids for e in nodes[i].evidence)),
                judgment="raw" if p.evidence_ids else "memory")
                for p in response.positives if valid_positive(p))
            self.audits.append(dict(operation="dependencies", target=target, selected=sorted(selected),
                generation=generation, response=response.model_dump(mode="json"), identities=identity_results,
                raw_sources_read=sorted(raw_sources), memory_versions=[(i, nodes[i].version, nodes[i].basis_revision) for i in sorted(selected)]))
            if complete or selected == set(ranked):
                self._apply(searched=(target,), edges=edges, complete=complete)
                self.read_ids[target] = ()  # Negative reads do not become permanent prerequisites.
                if not complete:
                    self.gaps[target] = (response.missing or "incomplete search: " + nodes[target].text,)
                    self._stop(target, "search_incomplete")
                return
            limit *= self.config.expansion_factor
        self._apply(searched=(target,), complete=False)
        self.gaps[target] = (response.missing or "raw lookup exhausted: " + nodes[target].text,)
        self._stop(target, "search_incomplete")

    @staticmethod
    def _failure_reason(exc):
        from pydantic import ValidationError
        from contexthub.llm.retry_policy import ResponseParseError, ResponseSchemaError
        import httpx
        if isinstance(exc, (ValidationError, ContractError, ResponseParseError, ResponseSchemaError)):
            return "format_failure"
        if isinstance(exc, (httpx.HTTPError, TimeoutError)):
            return "network_error"
        return "internal_error"

    async def _checked_dependencies(self, target):
        try:
            await self._dependencies(target)
        except BaseException as exc:
            self._stop(target, self._failure_reason(exc), ("dependency",))
            raise

    async def _discover_targets(self, targets):
        fresh = tuple(t for t in targets if self._nodes()[t].status == "fresh")
        if fresh:
            self._apply(invalidated=fresh)
        if not self.config.paired_rq1:
            for target in targets:
                await self._checked_dependencies(target)
            return
        # Two searches start from exactly the same extracted, versioned inventory.
        # The operational arm is predeclared; never pick the better resulting graph.
        base, configured = self.state, self.config
        start_history = list(self.history)
        starting = deepcopy((self.evidence, self.read_ids, self.attempts, self.gaps, self.source_watches))
        paired, arm_support = {}, {}
        try:
            for arm in ("selective", "full-history"):
                self.state, self.history = base, list(start_history)
                self.evidence, self.read_ids, self.attempts, self.gaps, self.source_watches = deepcopy(starting)
                self.config = configured.model_copy(update={"search": arm})
                self.models.search_arm = arm
                self.models.reference = arm != configured.search
                for target in targets:
                    await self._checked_dependencies(target)
                paired[arm] = self.state
                arm_support[arm] = deepcopy((self.evidence, self.read_ids, self.attempts, self.gaps, self.source_watches))
            self.state = paired[configured.search]
            self.evidence, self.read_ids, self.attempts, self.gaps, self.source_watches = arm_support[configured.search]
            self.audits.append(dict(operation="paired_rq1", basis_sha256=canonical_hash(base),
                targets=list(targets), operational_arm=configured.search,
                evidence_policy=configured.evidence_policy,
                states={arm:state.model_dump(mode="json") for arm,state in paired.items()},
                evidence_registries={arm: [e.model_dump(mode="json") for e in support[0].values()]
                                     for arm, support in arm_support.items()}))
        finally:
            self.config = configured
            self.models.search_arm = configured.search
            self.models.reference = False

    async def _repair(self, target):
        nodes = self._nodes()
        parents = {e.upstream for e in self.state.edges if e.downstream == target}
        if self._already_stopped(target):
            return
        if target in self.state.pending_targets or any(nodes[p].status != "fresh" for p in parents):
            self._stop(target, "search_incomplete" if target in self.state.pending_targets else "upstream_unresolved")
            return
        previous, full_payload = None, None
        actions, reason = [], "actions_exhausted"
        try:
            while True:
                decision = choose_next(self.config.profile, previous, legal=True, stop_reason=None, config=self.config.planner)
                self.audits.append(dict(operation="planner", target=target, decision=decision.model_dump(mode="json")))
                if decision.action is None:
                    self.attempts.setdefault(target, {})["budget_stop"] = decision.reason
                    self._stop(target, reason, actions)
                    return
                action = decision.action
                actions.append(action)
                previous = action
                nodes = self._nodes()
                visible_ids = self._related(target) if action == "M1" else set(nodes)
                evidence = tuple(dict.fromkeys([e for edge in self.state.edges if edge.downstream == target for e in edge.evidence]
                    + [e for e in self.source_watches.get(target, ())]
                    + [e for i in sorted(visible_ids) for e in nodes[i].evidence]
                    + [e for n in self.before.nodes if n.id in visible_ids for e in n.evidence]))
                payload = self._payload(nodes[target].model_dump(mode="json"), evidence,
                                        full=action != "M1", memory_ids=visible_ids)
                payload["previous_session"] = [n.model_dump(mode="json") for n in self.before.nodes if n.id in visible_ids]
                if action == "M2":
                    full_payload = deepcopy(payload)
                elif action == "M3":
                    payload = deepcopy(full_payload)  # M2/M3: exactly the same frozen evidence.
                visible_ids = {n["id"] for n in payload["memory"]}
                package_hash = canonical_hash({k: payload[k] for k in ("sources", "evidence", "memory", "graph")})
                self.audits.append(dict(operation="generate", target=target, action=action,
                    evidence_package=package_hash, sent_sources=[r["source_id"] for r in payload["sources"]]))
                record = self.attempts.setdefault(target, {})
                record["read_sources"] = sorted(set(record.get("read_sources", ())) | {r["source_id"] for r in payload["sources"]})
                role = "strong" if action in ("M3", "full/strong") else "base"
                generated = await self._ask(role, "generate", payload, Repair, stage="P2")
                if generated.outcome == "unresolved":
                    clues = tuple(dict.fromkeys(c.strip() for c in generated.search_clues if 2 <= len(c.strip()) <= 200 and terms(c)))
                    self.gaps[target] = clues or ((generated.missing,) if generated.missing else (nodes[target].text,))
                    reason = "missing_information"
                    continue
                allowed = {e["id"] for e in payload["evidence"]}
                if (not generated.text or not generated.evidence_ids
                        or not set(generated.evidence_ids) <= allowed or not set(generated.read_ids) <= visible_ids
                        or generated.kind == "uncertain"
                        or (generated.outcome == "unchanged" and generated.kind != payload["target"]["kind"])
                        or (generated.outcome == "unchanged") != (generated.text == payload["target"]["text"])):
                    reason = "format_failure"
                    continue
                # Both outcomes revise candidate support BEFORE final verification.
                # Content version is unchanged for a basis-only revision.
                old = nodes[target]
                refs = tuple(dict.fromkeys(generated.evidence_ids))
                new_reads = set(generated.read_ids) - parents - {target}
                previous_nodes = {n.id: n for n in self.before.nodes}
                drift = any(p not in previous_nodes or previous_nodes[p] != nodes[p] for p in parents)
                content_changed = (generated.text, generated.kind) != (old.text, old.kind)
                basis_changed = content_changed or refs != old.evidence or bool(new_reads) or drift
                if basis_changed:
                    self._apply(edits=(Edit(id=target, expected_version=old.version,
                        text=generated.text, kind=generated.kind, evidence=refs,
                        basis_only=not content_changed),), complete=False)
                    self.read_ids[target] = generated.read_ids
                    # Raw-only provenance gets an auditable lexical watch. This is
                    # a wakeup locator, never a confirmed semantic prerequisite.
                    self.source_watches[target] = refs
                    self.audits.append(dict(operation="basis_revision", target=target,
                        content_changed=content_changed, old_evidence=old.evidence,
                        new_evidence=refs, candidate_reads=generated.read_ids,
                        basis_revision=self._nodes()[target].basis_revision))
                    await self._index()
                    await self._discover_targets((target,))
                parents = {e.upstream for e in self.state.edges if e.downstream == target}
                if target in self.state.pending_targets or any(self._nodes()[p].status != "fresh" for p in parents):
                    self._stop(target, "search_incomplete" if target in self.state.pending_targets else "upstream_unresolved", actions)
                    return
                # Discovery may expose support absent from M1. Never silently
                # expand the verifier; move to the next explicit stage instead.
                frozen = {n["id"]: n for n in payload["memory"]}
                if any(p not in frozen or frozen[p] != self._nodes()[p].model_dump(mode="json") for p in parents):
                    reason = "missing_information"
                    continue
                final_edges = [e.model_dump(mode="json") for e in self.state.edges if e.downstream == target]
                verdict = await self._verify(self._nodes()[target].model_dump(mode="json"), refs,
                    package=payload, extra=dict(proposal=generated.model_dump(mode="json"),
                        final_dependencies=final_edges, upstream_ids=sorted(parents)))
                if not self._passed(verdict):
                    reason = "actions_exhausted"
                    continue
                if not parents and (self._nodes()[target].kind == "derived" or not verdict.independent):
                    reason = "missing_information"
                    continue
                self._proof(target, verdict)
                self.gaps.pop(target, None)
                self._stop(target, "verified", actions)
                return
        except BaseException as exc:
            self._stop(target, self._failure_reason(exc), actions)
            raise

    async def _save(self, status):
        old = self.checkpoint
        checkpoint = old.model_copy(update=dict(revision=old.revision + 1, previous_sha256=canonical_hash(old),
            status=status, state=self.state, evidence=tuple(self.evidence.values()),
            vectors=tuple(self.vectors.values()), history=tuple(self.history), audits=tuple(self.audits),
            call_ids=tuple(self.models.call_ids), attempts=deepcopy(self.attempts),
            read_ids=dict(self.read_ids), source_watches=dict(self.source_watches), gaps=dict(self.gaps)))
        await self.store.save(checkpoint, canonical_hash(old))
        self.checkpoint = checkpoint

    async def ingest_session(self, source):
        if self._busy:
            raise ContractError("session_concurrent_ingestion")
        source = SessionInput.model_validate(source.model_dump(mode="json"))
        self._busy = True
        try:
            return await self._ingest(source)
        finally:
            self._busy = False

    async def _ingest(self, source):
        prior = await self.store.load()
        if prior and (prior.status not in ("complete", "unresolved")
                      or prior.configuration_sha256 != self.config.fingerprint()):
            raise ContractError("session_previous_incomplete_or_config_drift")
        if source.ordinal != (len(prior.sources) if prior else 0):
            raise ContractError("session_source_order")
        from .session_state import SessionState
        self.before = prior.state if prior else SessionState()
        self.state = self.before
        self.evidence = {e.id: e for e in prior.evidence} if prior else {}
        self.vectors = {v.node_id: v for v in prior.vectors} if prior else {}
        self.audits, self.history = [], list(prior.history) if prior else []
        self.read_ids = dict(prior.read_ids) if prior else {}
        self.attempts = deepcopy(prior.attempts) if prior else {}
        self.source_watches = dict(prior.source_watches) if prior else {}
        self.gaps = dict(prior.gaps) if prior else {}
        self.models.ordinal = source.ordinal
        self.models.call_ids = []
        checkpoint = Checkpoint(stream_id=self.store.stream_id, account_id=self.store.account_id,
            configuration_sha256=self.config.fingerprint(), revision=prior.revision+1 if prior else 0,
            previous_sha256=canonical_hash(prior) if prior else None, status="processing",
            sources=(*prior.sources, source) if prior else (source,), state=self.state,
            evidence=tuple(self.evidence.values()), history=tuple(self.history), vectors=tuple(self.vectors.values()),
            attempts=deepcopy(self.attempts), read_ids=dict(self.read_ids), source_watches=dict(self.source_watches), gaps=dict(self.gaps))
        await self.store.save(checkpoint, canonical_hash(prior) if prior else None)
        self.checkpoint = checkpoint
        try:
            async with asyncio.timeout(self.config.session_timeout_seconds):
                view, registry = extraction_evidence_view(source)
                self.audits.append(dict(operation="extraction_evidence", registry={
                    ident: evidence.model_dump(mode="json") for ident, evidence in registry.items()}))
                extraction = await self._ask("extraction", "extract", dict(
                    session=view, allowed_evidence_ids=list(registry),
                    allowed_memory_ids=[n.id for n in self.state.nodes],
                    memory=[self._memory_fact(n) for n in self.state.nodes]),
                    Extraction, stage="P1")
                nodes = self._nodes()
                if not set(extraction.revisit_ids) <= nodes.keys():
                    raise ContractError("session_revisit_unknown_target")
                # Validate the WHOLE batch before touching state, evidence or read IDs.
                staged, staged_evidence, staged_reads, seen = [], {}, {}, set()
                for index, item in enumerate(extraction.items):
                    if item.action == "add":
                        if item.target_id is not None:
                            raise ContractError("session_add_cannot_target_old")
                        ident = "n_" + uuid5(self.store.stream_id, f"{source.ordinal}:{index}").hex
                    else:
                        ident = item.target_id
                        if ident not in nodes or nodes[ident].status == "deleted":
                            raise ContractError("session_edit_unknown_target")
                    if ident in seen:
                        raise ContractError("session_duplicate_binding")
                    if not set(item.read_memory_ids) <= nodes.keys():
                        raise ContractError("session_extraction_memory_not_supplied")
                    seen.add(ident)
                    if not set(item.evidence_ids) <= registry.keys():
                        raise ContractError("session_extraction_evidence_not_supplied")
                    bound = [registry[evidence_id] for evidence_id in item.evidence_ids]
                    refs = tuple(dict.fromkeys(e.id for e in bound))
                    staged_evidence.update((e.id, e) for e in bound)
                    staged_reads[ident] = item.read_memory_ids
                    staged.append((ident, item, refs))
                edits, direct = [], []
                self.evidence.update(staged_evidence)
                for ident, item, refs in staged:
                    deleted = False
                    if item.action == "delete":
                        candidate = nodes[ident].model_copy(update=dict(text=item.text, evidence=refs,
                            version=nodes[ident].version + 1, status="unresolved"))
                        verdict = await self._verify_ingest(candidate, action="delete", previous=nodes[ident],
                                                            candidate_read_ids=item.read_memory_ids)
                        deleted = self._ingest_passed(verdict)
                    kind = "uncertain" if item.action == "delete" and not deleted else item.kind
                    edits.append(Edit(id=ident, expected_version=nodes[ident].version if ident in nodes else 0,
                        text=item.text, kind=kind, evidence=tuple(refs), deleted=deleted))
                    if kind in ("assertion", "rule") and not deleted:
                        direct.append(ident)
                self._apply(edits=edits, complete=False)
                self.read_ids.update(staged_reads)
                await self._index()
                for ident in direct:
                    node = self._nodes()[ident]
                    if self.config.explicit_rules and self._literal_user_rule(node):
                        verdict = Verdict(applicable=True, supported=True, conflicts_resolved=True,
                            kind_confirmed=True, independent=True, evidence_ids=node.evidence)
                        passed = self._passed(verdict)
                        refs = node.evidence
                        scope = "deterministic-literal-rule"
                    else:
                        verdict = await self._verify_ingest(node, previous=nodes.get(ident))
                        self.read_ids[ident] = tuple(dict.fromkeys((*self.read_ids.get(ident, ()), *verdict.dependency_ids)))
                        passed = self._ingest_passed(verdict)
                        refs = tuple(dict.fromkeys(e for i in verdict.memory_ids for e in self._nodes()[i].evidence))
                        scope = "candidate-consistency-v2"
                    # Existing parents must go through dependency discovery; a
                    # standalone-ingest judgment cannot erase those edges.
                    if passed and verdict.independent and not any(e.downstream == ident for e in self.state.edges):
                        self._apply(searched=(ident,))
                        self.read_ids[ident] = ()
                        self._proof(ident, verdict, evidence_ids=refs, verification_scope=scope)
                targets = {e.id for e in edits if self._nodes()[e.id].status != "deleted"}
                targets |= set(extraction.revisit_ids)
                targets |= self._wake(source, edits)
                # New material also searches backwards for potentially affected
                # old targets; it isn't only new-target -> old-upstream discovery.
                reverse_targets = set()
                for edit in edits:
                    if edit.id in self.vectors:
                        ranked = self._rank(edit.id)
                        reverse_targets.update(i for i in ranked if overlap(edit.text, self._nodes()[i].text)
                            and (edit.kind == "rule" or i in {n.id for n in self.before.nodes})
                            and not (edit.kind != "rule" and self._literal_user_rule(self._nodes()[i])))
                targets.update(reverse_targets)
                discover = []
                for target in sorted(targets):
                    node = self._nodes()[target]
                    if node.status == "deleted" or node.kind == "uncertain":
                        continue
                    if node.status == "fresh" and target in direct and target not in extraction.revisit_ids and target not in reverse_targets:
                        continue
                    if not self._already_stopped(target):
                        discover.append(target)
                await self._discover_targets(tuple(discover))
                if not extraction.routing_complete:
                    # Unknown reverse routing cannot certify unaffected old values.
                    all_ids = tuple(n.id for n in self.state.nodes if n.status != "deleted")
                    self._apply(searched=all_ids, complete=False)
                    for ident in all_ids:
                        self.gaps[ident] = ("reverse routing incomplete: " + self._nodes()[ident].text,)
                        self._stop(ident, "search_incomplete")
                for _ in range(len(self.state.nodes)):
                    start = canonical_hash(self.state)
                    if self.config.explicit_rules:
                        # Only raw-user, source-bound rules can enter the fast path.
                        eligible = all(self._literal_user_rule(n) for n in self.state.nodes
                                       if n.kind == "rule" and parse_rule(n.text))
                        if eligible:
                            result, applications, _ = maintain_explicit_changes(self.before, self.state)
                            for application in applications:
                                target = application.proof.target
                                self._remember(self._nodes()[target])
                                self.audits.append(dict(operation="literal_repair", proof=application.proof.model_dump(mode="json")))
                            self.state = result
                    for node in tuple(self.state.nodes):
                        if self._nodes()[node.id].status in ("stale", "unresolved") and node.kind != "uncertain":
                            if node.id in self.state.pending_targets and not self._already_stopped(node.id):
                                await self._discover_targets((node.id,))
                            await self._repair(node.id)
                    if canonical_hash(self.state) == start:
                        break
                await self._index()
                status = "complete" if all(n.status in ("fresh", "deleted") for n in self.state.nodes) else "unresolved"
                await self._save(status)
        except BaseException as exc:
            # Never serialize half-built working collections into the failure checkpoint.
            # The processing checkpoint contains the last complete memory plus this source.
            self.failure = dict(type=type(exc).__name__, message=str(exc),
                                code=getattr(exc, "code", type(exc).__name__),
                                call_ids=[str(i) for i in self.models.call_ids])
            from pydantic import ValidationError
            if isinstance(exc, ValidationError):
                self.failure["validation_errors"] = exc.errors(include_input=False,
                    include_context=False, include_url=False)
            self.audits.append(dict(operation="failed", reason=type(exc).__name__, error=deepcopy(self.failure)))
            try:
                durable = await self.store.load()
                if (durable is None or canonical_hash(durable) != canonical_hash(self.checkpoint)
                        or durable.status != "processing"):
                    raise ContractError("session_failure_checkpoint_drift")
                failed = durable.model_copy(update=dict(revision=durable.revision + 1,
                    previous_sha256=canonical_hash(durable), status="failed",
                    audits=tuple(self.audits), call_ids=tuple(self.models.call_ids)))
                await self.store.save(failed, canonical_hash(durable))
                self.checkpoint = failed
                self.state = failed.state
                self.evidence = {e.id: e for e in failed.evidence}
                self.vectors = {v.node_id: v for v in failed.vectors}
                self.history = list(failed.history)
                self.read_ids = dict(failed.read_ids)
                self.attempts = deepcopy(failed.attempts)
                self.source_watches = dict(failed.source_watches)
                self.gaps = dict(failed.gaps)
            except BaseException as save_error:
                # A storage outage cannot be repaired here. Keep the original exception
                # and expose the secondary failure, never report a successful close.
                self.failure["checkpoint_save_error"] = dict(type=type(save_error).__name__,
                    code=getattr(save_error, "code", type(save_error).__name__))
                exc.add_note("failed checkpoint not persisted: " + type(save_error).__name__)
            raise
        snapshot = await self.snapshot()
        return dict(ordinal=source.ordinal, source_id=source.source_id, source_version=source.source_version,
            content_sha256=source.content_sha256, revision=self.checkpoint.revision,
            status=self.checkpoint.status, state_sha256=canonical_hash(snapshot),
            call_ids=[str(i) for i in self.models.call_ids])
