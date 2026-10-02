"""Complete S4 pipeline. Explicitly composed, never default API assembly."""

import asyncio
from pydantic import Field
from itertools import combinations
from uuid import uuid4, uuid5
from contexthub.models.knowledge import (
    Contract,
    ContractError,
    GraphSnapshot,
    DependencyEdge,
    PendingDependency,
    PropositionArtifact,
    canonical_hash,
)
from contexthub.discovery.adjudication import AdjudicationConfig, EvidenceSet
from contexthub.discovery.search import SearchPolicy, SearchCandidate
from contexthub.discovery.stage0 import locate
from contexthub.discovery.identity_routing import IdentityRoutingConfig, route_pair
from contexthub.discovery.adjudication import IdentityResolution, Adjudication
from contexthub.discovery.contracts import DiscoveryReport, NodeMapping, Decision, encode_configuration
from contexthub.discovery.nodes import materialize_propositions
from contexthub.discovery._work import LocalOperation, MeteredDB
from contexthub.discovery.extraction import error_code
from contexthub.services.artifact_service import ArtifactService


from contexthub.discovery.semantic_runner import SemanticConfig


class DiscoveryConfig(Contract):
    search: SearchPolicy
    adjudication: AdjudicationConfig
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    identity_routing: IdentityRoutingConfig | None = Field(default=None, exclude_if=lambda v: v is None)

    semantic: SemanticConfig | None = Field(default=None, exclude_if=lambda v: v is None)

    def check(self):
        if self.semantic is not None and (self.identity_routing is None or self.identity_routing.unknown_policy != "advisory"):
            raise ContractError("semantic_requires_advisory_identity_routing")
        if self.identity_routing is not None:
            ner = self.adjudication.stage0.spacy_ner
            if ner is None or not ner.allowed_entity_labels:
                raise ContractError("demand_identity_requires_explicit_name_labels")
        if self.search.lambda_p1 != self.adjudication.lambda_p1:
            raise ContractError("price_config_mismatch")


class DiscoveryService:
    def __init__(self, config, *, search, adjudicator, recorder, artifacts=None, semantic_runner=None):
        config.check()
        if config.adjudication != adjudicator.config:
            raise ContractError("adjudicator_config_mismatch")
        self.config, self.search, self.adjudicator = config, search, adjudicator
        if (config.semantic is None) != (semantic_runner is None):
            raise ContractError("semantic_runner_configuration_missing")
        if semantic_runner is not None and (semantic_runner.config != config.semantic or semantic_runner.routing != config.identity_routing or semantic_runner.adjudicator is not adjudicator):
            raise ContractError("semantic_runner_configuration_mismatch")
        self.semantic_runner = semantic_runner
        self.recorder = recorder
        self.artifacts = artifacts or ArtifactService()

    async def discover(
        self, db, shared_ref, history, *, identity, report_id=None, prior_extractions=()
    ):
        async with asyncio.timeout(self.config.timeout_seconds):
            return await self._discover(
                db,
                shared_ref,
                history,
                identity=identity,
                report_id=report_id,
                prior_extractions=prior_extractions,
            )

    async def _discover(
        self, db, shared_ref, history, *, identity, report_id=None, prior_extractions=()
    ):
        if identity.config_sha256 != canonical_hash(self.config):
            raise ContractError("discovery_config_mismatch")
        if (
            identity.account_id != history.account_id
            or shared_ref.account_id != history.account_id
        ):
            raise ContractError("tenant_mismatch")
        work = LocalOperation(
            self.recorder,
            identity,
            {
                "shared": shared_ref.model_dump(mode="json"),
                "history": history.model_dump(mode="json"),
                "prior_extractions": [
                    r.model_dump(mode="json") for r in prior_extractions
                ],
            },
        )
        await work.start()
        db = MeteredDB(db, work)
        call_ids = [identity.call_id]
        pages, decisions, reasons, mappings = [], [], [], []
        evidence_recoveries = []
        identity_routes = []
        semantic_selections = []
        semantic_index = None
        # Semantic retrieval indexes the full admitted catalog. The fact shortlist,
        # not a recency source window, distinguishes the selective arm.
        catalog_policy = self.config.search.model_copy(update={"strategy":"full-history"}) if self.config.semantic else self.config.search
        states = ["awaiting_extraction"]
        report_id = report_id or uuid4()

        def child(label, config, role="local"):
            call = identity.model_copy(
                update={
                    "call_id": uuid5(identity.call_id, label),
                    "operation": label,
                    "model_role": role,
                    "config_sha256": canonical_hash(config),
                }
            )
            call_ids.append(call.call_id)
            return call

        async def consume(page):
            pages.append(page)
            while page.cursor is not None:
                page = await self.search.search(
                    db,
                    shared_ref,
                    history,
                    catalog_policy,
                    identity=child("search-" + str(len(pages)), catalog_policy),
                    cursor=page.cursor,
                )
                pages.append(page)

        try:
            artifact = await self.artifacts.get(db, shared_ref)
            if not isinstance(artifact, PropositionArtifact):
                raise ContractError("reference_kind_mismatch")
            excluded_extractions = [shared_ref] if artifact.unresolved else []
            carriers = {}
            states.append("awaiting_search")
            await consume(
                await self.search.search(
                    db,
                    shared_ref,
                    history,
                    catalog_policy,
                    identity=child("search-0", catalog_policy),
                )
            )
            generation = 0
            # Every expansion strictly increases scope. S3 bounds its own fault
            # recovery. At most len(manifest)+1 semantic evidence generations.
            while True:
                last = pages[-1]
                if not last.complete:
                    reasons.extend(i.code for i in last.issues)
                    reasons.append("search_incomplete")
                    break
                carriers.update(dict(await materialize_propositions(db, artifact)))
                candidates = {c.id: c for page in pages for c in page.candidates}
                batch = [
                    c for c in candidates.values() if c.kind == "batch_proposition"
                ]
                if {c.id for c in batch} != {p.id for p in artifact.propositions}:
                    reasons.append("batch_candidates_incomplete")
                    break
                # Expand historical source candidates to their SAME shared,
                # previously extracted propositions. Never re-extract per arm.
                visible = set(last.read_sources)
                represented = set(artifact.sources)
                for prior_ref in prior_extractions:
                    prior = await self.artifacts.get(db, prior_ref)
                    if (
                        not isinstance(prior, PropositionArtifact)
                        or prior.status not in ("complete", "partial")
                    ):
                        reasons.append("prior_extraction_pending")
                        continue
                    if prior.unresolved and prior_ref not in excluded_extractions:
                        excluded_extractions.append(prior_ref)
                    admitted_props = [
                        p
                        for p in prior.propositions
                        if all(e.source in visible for e in p.evidence)
                    ]
                    if not admitted_props:
                        continue
                    for p in admitted_props:
                        sources = tuple(dict.fromkeys(e.source for e in p.evidence))
                        for ref in sources:
                            await self.search._admit_source(db, ref, history, {})
                        carriers.update(
                            dict(
                                await materialize_propositions(
                                    db, prior, proposition_ids={p.id}
                                )
                            )
                        )
                        candidates[p.id] = SearchCandidate(
                            id=p.id,
                            kind="batch_proposition",
                            proposition_id=p.id,
                            sources=sources,
                            source_versions=(),
                            text=p.text,
                            evidence=p.evidence,
                            explicit_provenance=(),
                        )
                        represented.update(sources)
                candidates = {
                    i: c
                    for i, c in candidates.items()
                    if c.kind != "history_source" or c.sources[0] not in represented
                }
                if len(candidates) < 2 and not last.at_max_scope:
                    page = await self.search.expand(
                        db,
                        pages,
                        history,
                        catalog_policy,
                        identity=child(
                            "empty-expand-" + str(generation), self.config.search
                        ),
                        reason="no_comparable_candidate_in_local_scope",
                    )
                    if page is None or generation >= len(history.entries):
                        reasons.append("no_comparable_candidate")
                        break
                    await consume(page)
                    generation += 1
                    continue
                states.append("awaiting_adjudication")
                try:
                    read_result = await self.search.read_evidence(
                        db, last.read_sources, history
                    )
                    evidence = read_result.evidence
                    evidence_recoveries.extend(read_result.recoveries)
                except Exception as exc:
                    reasons.append("evidence_read_" + error_code(exc))
                    break
                # Extraction spans retain provenance and are also admissible.
                evidence = tuple(
                    dict.fromkeys((*evidence, *(e for c in batch for e in c.evidence)))
                )
                bundle = EvidenceSet(
                    evidence=evidence,
                    complete=True,
                    at_max_scope=last.at_max_scope,
                    issues=(),
                )
                mappings = []
                for c in candidates.values():
                    node = (
                        carriers[c.id]
                        if c.kind == "batch_proposition"
                        else c.source_versions[0]
                    )
                    refs = (
                        c.evidence
                        if c.evidence
                        else tuple(e for e in evidence if e.source in c.sources)
                    )
                    mappings.append(
                        NodeMapping(
                            candidate_id=c.id,
                            proposition_id=c.proposition_id,
                            node=node,
                            text=c.text,
                            evidence=refs,
                        )
                    )
                read_more = False
                round_decisions = []
                # Entity identity never deletes a fact/provenance pair. SAME
                # requests a wider candidate envelope; no entity IDs are merged.
                mentions = locate(self.config.adjudication.stage0, evidence)
                identity_cache = {}  # Only this bundle/generation; no cross-scope reuse.
                global_pairs = combinations(mentions, 2) if self.config.identity_routing is None else ()
                for index, (left, right) in enumerate(global_pairs):
                    ident = child(
                        f"entity-{generation}-{index}", self.config.adjudication
                    )
                    result = await self.adjudicator.judge(
                        db,
                        kind="entity",
                        left=left.text,
                        right=right.text,
                        mentions=(left, right),
                        bundle=bundle,
                        identity=ident,
                        generation=generation,
                    )
                    call_ids.append(uuid5(ident.call_id, "adjudication-local"))
                    call_ids.extend(t.call_id for t in result.traces)
                    decisions.append(
                        Decision(
                            upstream_id=uuid5(left.evidence.source.id, left.text),
                            downstream_id=uuid5(right.evidence.source.id, right.text),
                            result=result,
                        )
                    )
                    if result.action == "read_more" or (
                        result.verdict == "SAME" and not last.at_max_scope
                    ):
                        read_more = True
                        break
                    if result.action == "pending":
                        reasons.append("entity_" + result.reason)
                if self.semantic_runner is not None:
                    sd, sf, sr, ss, semantic_index, sc = await self.semantic_runner.run(
                        db, targets=batch, candidates=candidates, evidence=evidence, history=history,
                        identity=child("semantic-round", self.config.semantic),
                        full_history=self.config.search.strategy == "full-history")
                    decisions.extend(sd)
                    round_decisions.extend(sf)
                    identity_routes.extend(sr)
                    semantic_selections.extend(ss)
                    call_ids.extend(sc)
                    break
                if not read_more:
                    for target in batch:
                        for candidate in candidates.values():
                            if candidate.id == target.id:
                                continue
                            # Source->its own extracted proposition is a mapping,
                            # not an inferred fact dependency.
                            if (
                                candidate.kind == "history_source"
                                and candidate.sources[0] in target.sources
                            ):
                                continue
                            resolutions = []
                            unresolved_identity = False
                            if self.config.identity_routing is not None:
                                routed, audit = route_pair(candidate, target, mentions,
                                                          self.config.identity_routing, generation)
                                reused = 0
                                processed_keys = []
                                for key, left, right, route_reason in routed:
                                    processed_keys.append(key)
                                    if key in identity_cache:
                                        er = identity_cache[key]
                                        reused += 1
                                    else:
                                        ei = child(f"demand-entity-{generation}-{key}", self.config.adjudication)
                                        er = await self.adjudicator.judge(db, kind="entity",
                                            left=left.text, right=right.text, mentions=(left, right),
                                            bundle=bundle, identity=ei, generation=generation)
                                        identity_cache[key] = er
                                        call_ids.append(uuid5(ei.call_id, "adjudication-local"))
                                        call_ids.extend(t.call_id for t in er.traces)
                                        decisions.append(Decision(
                                            upstream_id=uuid5(report_id, canonical_hash(left)),
                                            downstream_id=uuid5(report_id, canonical_hash(right)), result=er))
                                    if er.action == "read_more" or (er.verdict == "SAME" and not last.at_max_scope):
                                        read_more = True
                                        break
                                    advisory_unknown = (self.config.identity_routing.unknown_policy == "advisory"
                                                        and er.verdict == "UNKNOWN" and er.action == "pending")
                                    if er.action != "complete" and not advisory_unknown:
                                        unresolved_identity = True
                                    else:
                                        resolutions.append(IdentityResolution(mentions=(left, right), result=er))
                                identity_routes.append(audit.model_copy(update={"reused_resolutions": reused, "processed_resolution_keys": tuple(processed_keys)}))
                                if read_more:
                                    break
                                if unresolved_identity:
                                    # Preserve a pending dependency; never turn an identity
                                    # failure or NER filter into a negative dependency verdict.
                                    d = Decision(upstream_id=candidate.id, downstream_id=target.id,
                                        result=Adjudication(kind="dependency", verdict="UNKNOWN", action="pending",
                                            reason="required_identity_unresolved", evidence_sha256=canonical_hash(bundle),
                                            traces=(), generation=generation))
                                    decisions.append(d)
                                    round_decisions.append(d)
                                    continue
                            explicit = any(
                                p.proposition_id == target.id
                                and p.source in candidate.sources
                                for p in history.provenance
                            )
                            ident = child(
                                f"pair-{generation}-{candidate.id}-{target.id}",
                                self.config.adjudication,
                            )
                            result = await self.adjudicator.judge(
                                db,
                                kind="dependency",
                                left=candidate.text,
                                right=target.text,
                                bundle=bundle,
                                identity=ident,
                                generation=generation,
                                explicit_provenance=explicit,
                                **({"identity_resolutions": tuple(resolutions)} if self.config.identity_routing is not None else {}),
                            )
                            call_ids.append(uuid5(ident.call_id, "adjudication-local"))
                            call_ids.extend(t.call_id for t in result.traces)
                            decision = Decision(
                                upstream_id=candidate.id,
                                downstream_id=target.id,
                                result=result,
                            )
                            decisions.append(decision)
                            round_decisions.append(decision)
                            if result.action == "read_more":
                                read_more = True
                                break
                        if read_more:
                            break
                if not read_more:
                    break
                if generation >= len(history.entries):
                    reasons.append("expansion_limit")
                    break
                page = await self.search.expand(
                    db,
                    pages,
                    history,
                    catalog_policy,
                    identity=child("expand-" + str(generation), catalog_policy),
                    reason="adjudication_read_more",
                )
                if page is None:
                    reasons.append("missing_premise_at_max")
                    break
                await consume(page)
                generation += 1
                # No old/new evidence judgments are combined. All pairs re-run.
                reasons = []
            nodes = tuple(dict.fromkeys(m.node for m in mappings))
            by_id = {m.candidate_id: m for m in mappings}
            edges, pending = [], []
            if pages[-1].complete and not any(
                r in ("search_incomplete", "expansion_limit") for r in reasons
            ):
                for decision in locals().get("round_decisions", []):
                    r = decision.result
                    up, down = (
                        by_id[decision.upstream_id].node,
                        by_id[decision.downstream_id].node,
                    )
                    if up == down:
                        continue
                    if r.action == "not_selected":
                        # Sparse cascade omission is not an independence proof.
                        continue
                    if r.action != "complete":
                        pending.append(
                            PendingDependency(
                                upstream=up, downstream=down, reason=r.reason
                            )
                        )
                        reasons.append(r.reason)
                    elif r.verdict == "DEPENDS":
                        j = r.accepted
                        refs = tuple(
                            dict.fromkeys(
                                (
                                    *j.citations,
                                    *(
                                        ref
                                        for step in j.steps
                                        for ref in (*step.premises, step.conclusion)
                                    ),
                                )
                            )
                        )
                        edges.append(
                            DependencyEdge(
                                upstream=up,
                                downstream=down,
                                semantic_basis=j.semantic_basis,
                                evidence=refs,
                                provenance=tuple(
                                    dict.fromkeys(p for e in refs for p in e.provenance)
                                ),
                            )
                        )

            # Any cyclic accepted claims remain pending; never pick a direction
            # by insertion order and silently claim a complete DAG.
            def reaches(start, target, seen):
                if start == target:
                    return True
                if start in seen:
                    return False
                return any(
                    reaches(e.downstream, target, seen | {start})
                    for e in edges
                    if e.upstream == start
                )

            cyclic = [e for e in edges if reaches(e.downstream, e.upstream, set())]
            for edge in cyclic:
                pending.append(
                    PendingDependency(
                        upstream=edge.upstream,
                        downstream=edge.downstream,
                        reason="cyclic_claims",
                    )
                )
            if cyclic:
                reasons.append("cyclic_claims")
            edges = [e for e in edges if e not in cyclic]
            graph = GraphSnapshot(
                account_id=identity.account_id,
                id=uuid5(report_id, "graph"),
                graph_version=1,
                nodes=nodes,
                edges=tuple(edges),
                pending=tuple(pending),
                config_sha256=canonical_hash(self.config),
            )
            await self.artifacts.seal(db, graph)
            if artifact.status not in ("complete", "partial"):
                reasons.append("extraction_pending")
            status = "pending" if reasons else "complete"
            states.append(status)
            report = DiscoveryReport(
                account_id=identity.account_id,
                id=report_id,
                execution_id=identity.execution_id,
                code_version=identity.code_version,
                config_sha256=canonical_hash(self.config),
                configuration=encode_configuration(self.config),
                identity_routes=tuple(identity_routes),
                semantic_selections=tuple(semantic_selections),
                semantic_index=semantic_index,
                excluded_unresolved_extractions=tuple(excluded_extractions),
                shared_extraction=shared_ref,
                prior_extractions=prior_extractions,
                graph=graph.reference(),
                history_sha256=canonical_hash(history),
                strategy=self.config.search.strategy,
                status=status,
                states=tuple(states),
                reasons=tuple(dict.fromkeys(reasons)),
                mappings=tuple(mappings),
                pages=tuple(pages),
                decisions=tuple(decisions),
                evidence_recoveries=tuple(evidence_recoveries),
                call_ids=tuple(dict.fromkeys(call_ids)),
            )
            await self.artifacts.seal(db, report)
            work.add("audit_records", len(decisions) + 1)
            await work.finish(report, reasons[0] if reasons else None)
            return report
        except Exception as exc:
            await work.finish({"report_id": str(report_id)}, error_code(exc))
            raise


def assemble_graph_snapshot(graphs, *, account_id, graph_id, config_sha256):
    """Compose batch graphs for downstream traversal, preserving exact versions.

    Conflicting node versions, edge claims or cycles reject the aggregate via
    GraphSnapshot validation. No candidate or UNKNOWN becomes a confirmed edge.
    """
    if any(g.account_id != account_id for g in graphs):
        raise ContractError("tenant_mismatch")
    return GraphSnapshot(
        account_id=account_id,
        id=graph_id,
        graph_version=1,
        nodes=tuple(dict.fromkeys(n for g in graphs for n in g.nodes)),
        edges=tuple(dict.fromkeys(e for g in graphs for e in g.edges)),
        pending=tuple(dict.fromkeys(p for g in graphs for p in g.pending)),
        config_sha256=config_sha256,
    )


async def read_graph_handoff(db, report_ref):
    """Read an audited batch graph and its source-version -> carrier seeds.

    Consumers must inspect report.status. This only exposes traversal; it does
    not mark anything stale, run maintenance or treat pending as a negative.
    """
    artifacts = ArtifactService()
    report = await artifacts.get(db, report_ref)
    from contexthub.discovery.contracts import GraphHandoff
    if not isinstance(report, (DiscoveryReport, GraphHandoff)):
        raise ContractError("reference_kind_mismatch")
    graph = await artifacts.get(db, report.graph)
    seeds = {}
    for mapping in report.mappings:
        for evidence in mapping.evidence:
            snapshot = await artifacts.get(db, evidence.source)
            seeds.setdefault(snapshot.source, [])
            if mapping.node not in seeds[snapshot.source]:
                seeds[snapshot.source].append(mapping.node)
    downstream = {
        n: tuple(e.downstream for e in graph.edges if e.upstream == n)
        for n in graph.nodes
    }
    return report, graph, {k: tuple(v) for k, v in seeds.items()}, downstream
