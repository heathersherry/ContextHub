"""Opt-in semantic shortlist and target-batch execution inside discovery."""
from uuid import uuid5
from typing import Literal
from pydantic import Field
from contexthub.models.knowledge import Contract, canonical_hash
from contexthub.models.execution import PriceBasis
from contexthub.llm.recorded_embedding import EmbeddingConfig
from contexthub.llm.retry_policy import RecordedCall
from contexthub.discovery.semantic_candidates import RetrievalPolicy, DecisionReuse, select_facts
from contexthub.discovery.embedding_index import build_fact_index
from contexthub.discovery.adjudication import EvidenceSet,IdentityResolution
from contexthub.discovery.batch_adjudication import BatchAdjudicator
from contexthub.discovery.dependency_batch import BatchFact
from contexthub.discovery.identity_routing import route_pair
from contexthub.discovery.stage0 import locate


class SemanticConfig(Contract):
    execution: Literal["dual-weak-batch-v2", "sparse-cascade-v1"] = Field(
        default="dual-weak-batch-v2", exclude_if=lambda v: v == "dual-weak-batch-v2")
    embedding: EmbeddingConfig
    retrieval: RetrievalPolicy
    price: PriceBasis | None


def decision_context(account_id, target, candidates, history, bundle, configuration, routing):
    """Stable reuse scope; candidate exhaustion alone is not a new fact/version."""
    return canonical_hash(dict(account_id=account_id,target=canonical_hash(target),
        candidates=[canonical_hash(c) for c in candidates],history=canonical_hash(history),
        evidence=[canonical_hash(e) for e in bundle.evidence],complete=bundle.complete,issues=bundle.issues,
        configuration=canonical_hash(configuration),routing=canonical_hash(routing)))


class SemanticRunner:
    def __init__(self,config,*,embedding,adjudicator,recorder,routing):
        self.config,self.embedding,self.adjudicator,self.recorder,self.routing=config,embedding,adjudicator,recorder,routing
        self.batch=BatchAdjudicator(adjudicator)
        self.previous={}
        self.entity_cache={}

    async def run(self,db,*,targets,candidates,evidence,history,identity,full_history=False):
        if self.config.execution == "sparse-cascade-v1":
            from contexthub.discovery.sparse_cascade import run_cascade
            return await run_cascade(self, db, targets=targets, candidates=candidates, evidence=evidence,
                history=history, identity=identity, full_history=full_history)
        from contexthub.discovery.contracts import Decision
        decisions=[];final=[];routes=[];selections=[];calls=[]
        def child(label,cfg):
            result=identity.model_copy(update={'call_id':uuid5(identity.call_id,label),'config_sha256':canonical_hash(cfg)})
            return result
        # Cache is account-scoped and exact-candidate/model-bound. Index itself is
        # saved in the parent sealed report; no external globally shared cache.
        ix_identity=child('semantic-index',self.config.embedding)
        old=self.previous.get(identity.account_id)
        index=await build_fact_index(tuple(candidates.values()),history_sha256=canonical_hash(history),
            embedding_client=self.embedding,identity=ix_identity,recorder=self.recorder,
            price=self.config.price,previous=old)
        missing_count=sum(1 for c in candidates.values() if old is None or not any(
            canonical_hash(f.candidate)==canonical_hash(c) for f in old.facts))
        calls.extend(uuid5(ix_identity.call_id,f'embedding-batch-{s}') for s in range(0,missing_count,self.config.embedding.max_batch))
        self.previous[identity.account_id]=index
        for target in targets:
            # The source of this target is an extraction mapping, not a semantic edge.
            eligible={i:c for i,c in candidates.items() if c.kind!='history_source' or c.sources[0] not in target.sources}
            eligible[target.id]=target
            target_index=index.model_copy(update={'facts':tuple(f for f in index.facts if f.candidate.id in eligible)})
            required=tuple(c.id for c in eligible.values() if c.id!=target.id and any(
                p.proposition_id==target.id and p.source in c.sources for p in history.provenance))
            policy=self.config.retrieval
            if full_history:
                policy=policy.model_copy(update={'initial_candidates':max(1,len(eligible)-1)})
            selection=None;gap=None
            # Only this target in this invocation. Original decisions/traces keep
            # their generation and evidence hash; reuse never impersonates a call.
            memo={};memo_context=None
            while True:
                selection=select_facts(target_index,policy,target.id,required_ids=required,previous=selection,gap_vector=gap)
                selections.append(selection)
                if not selection.selected_ids: break
                chosen=[eligible[i] for i in selection.selected_ids]
                sources=set(target.sources)
                for c in chosen: sources.update(c.sources)
                bundle=EvidenceSet(evidence=tuple(e for e in evidence if e.source in sources),
                    complete=True,at_max_scope=selection.universe_exhausted,issues=())
                # at_max_scope describes candidate enumeration, not new evidence.
                # Bind full refs (including source/version/spans), not just text.
                context=decision_context(identity.account_id,target,eligible.values(),history,bundle,
                    self.batch.configuration,self.routing)
                if context!=memo_context:
                    memo={};memo_context=context
                mentions=locate(self.adjudicator.config.stage0,bundle.evidence)
                cache={}; references={}; needs_more=False; round_results=[]
                round_by_id={};reuse_audit=[];fresh=[]
                for c in chosen:
                    if c.id not in memo:
                        fresh.append(c);continue
                    original=memo[c.id];d=original;disposition='retained'
                    if d.result.action=='read_more':
                        if bundle.at_max_scope:
                            d=d.model_copy(update={'result':d.result.model_copy(update={
                                'action':'pending','reason':'missing_premise_at_max'})})
                            disposition='missing_at_max';decisions.append(d)
                        else:needs_more=True
                    round_by_id[c.id]=d
                    reuse_audit.append(DecisionReuse(candidate_id=c.id,
                        from_generation=original.result.generation,
                        decision_sha256=canonical_hash(original),context_sha256=context,
                        disposition=disposition))
                selection=selection.model_copy(update={'decision_reuses':tuple(reuse_audit)})
                selections[-1]=selection
                entity_needs_more=False
                for c in fresh:
                    pairs,audit=route_pair(c,target,mentions,self.routing,selection.generation)
                    refs=[];processed=[];reused=0
                    for key,left,right,_ in pairs:
                        processed.append(key)
                        if key in cache: er=cache[key];reused+=1
                        else:
                            ident=child(f'entity-{target.id}-{selection.generation}-{key}',self.adjudicator.config)
                            er=await self.adjudicator.judge(db,kind='entity',left=left.text,right=right.text,
                                mentions=(left,right),bundle=bundle,identity=ident,generation=selection.generation)
                            cache[key]=er
                            calls.extend([uuid5(ident.call_id,'adjudication-local'),*(t.call_id for t in er.traces)])
                            decisions.append(Decision(upstream_id=uuid5(left.evidence.source.id,canonical_hash(left)),
                                downstream_id=uuid5(right.evidence.source.id,canonical_hash(right)),result=er))
                        if er.action=='read_more':
                            entity_needs_more=True;needs_more=True;break
                        refs.append(IdentityResolution(mentions=(left,right),result=er))
                    routes.append(audit.model_copy(update={'processed_resolution_keys':tuple(processed),'reused_resolutions':reused}))
                    references[c.id]=tuple(refs)
                    if entity_needs_more: break
                if not entity_needs_more:
                    for number,ids in enumerate(selection.batches):
                        ids=tuple(i for i in ids if i not in memo)
                        if not ids:continue
                        ident=child(f'dependency-{target.id}-{selection.generation}-{number}',self.batch.configuration)
                        results=await self.batch.judge(db,target=BatchFact(id=target.id,text=target.text),
                            candidates=tuple(BatchFact(id=i,text=eligible[i].text) for i in ids),bundle=bundle,
                            identity=ident,generation=selection.generation,required_ids=tuple(i for i in ids if i in required),
                            identity_resolutions={i:references[i] for i in ids})
                        calls.append(uuid5(ident.call_id,'batch-checks'))
                        for i,r in results.items():
                            calls.extend(t.call_id for t in r.traces)
                            d=Decision(upstream_id=i,downstream_id=target.id,result=r)
                            decisions.append(d);round_by_id[i]=d;memo[i]=d
                        needs_more=needs_more or any(r.action=='read_more' for r in results.values())
                round_results=[round_by_id[c.id] for c in chosen if c.id in round_by_id]
                if not needs_more or selection.universe_exhausted:
                    final.extend(round_results);break
                # Retrieval query comes from missing-premise diagnostics, never
                # from task questions/labels. No truncation or rewriting of facts.
                gaps=sorted({m for d in round_results for t in d.result.traces if t.judgment for m in t.judgment.missing})
                gap=None
                if gaps:
                    ident=child(f'gap-{target.id}-{selection.generation}',self.config.embedding)
                    ident=ident.model_copy(update={'operation':'embed_evidence_gap','model_role':'embedding'})
                    vectors=await self.embedding.embed_batch((target.text+'\nMissing evidence:\n'+'\n'.join(gaps),),
                        call=RecordedCall(identity=ident,policy=self.config.embedding.retry,recorder=self.recorder,price=self.config.price))
                    calls.append(ident.call_id);gap=vectors[0]
        return decisions,final,routes,selections,index,tuple(dict.fromkeys(calls))
