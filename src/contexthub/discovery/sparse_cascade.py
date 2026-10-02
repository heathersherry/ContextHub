"""Sparse positive-edge cascade; explicit, bounded evidence-gap expansion."""
import re
from uuid import uuid5

from contexthub.discovery.adjudication import EvidenceSet
from contexthub.discovery.cascade_contracts import CascadeAudit, GapAudit
from contexthub.discovery.contracts import Decision
from contexthub.discovery.embedding_index import build_fact_index
from contexthub.discovery.semantic_candidates import select_facts
from contexthub.discovery.sparse_adjudication import SparseAdjudicator
from contexthub.discovery.stage0 import locate
from contexthub.discovery._work import LocalOperation, MeteredDB
from contexthub.discovery.extraction import error_code
from contexthub.llm.retry_policy import RecordedCall
from contexthub.models.knowledge import canonical_hash


def gap_key(gap):
    # Changing candidate number or search wording cannot repeat the same premise.
    return canonical_hash(' '.join(gap.missing_fact.casefold().split()))


def gap_leads(gap, *, registry, unexamined, candidates, bundle, seen):
    key=gap_key(gap)
    if key in seen:return 'repeated_gap',()
    if (len(set(gap.anchor_evidence_ids))!=len(gap.anchor_evidence_ids)
            or any(i not in registry for i in gap.anchor_evidence_ids)):
        return 'invalid_gap_anchor',()
    # Validate a specific search request; no score threshold certifies completeness.
    generic={'evidence','information','details','more','relevant','unknown','context','rule',
        'fact','facts','依据','信息','更多','相关','规则','事实','资料'}
    vague=generic|{'need','needed','missing','uncertain','maybe','not','sure','the','a','an','is','are','to','of','for','and','不知道','不确定'}
    premise_words=set(re.findall(r'[^\W_]+',gap.missing_fact.casefold()))
    if not premise_words-vague:return 'unspecified_missing_premise',()
    terms=[t.strip().casefold() for t in gap.search_terms if t.strip().casefold() not in generic]
    if not terms:return 'no_distinctive_search_term',()
    visible={canonical_hash(e.source) for e in bundle.evidence}
    # Availability is verifiable; semantic relevance remains retrieval's job.
    # Never turn a lexical overlap or similarity score into a completeness gate.
    leads=tuple(i for i in unexamined if any(canonical_hash(s) not in visible for s in candidates[i].sources))
    return ('new_source_available' if leads else 'no_new_source_lead'),leads



async def run_cascade(owner, db, *, targets, candidates, evidence, history, identity, full_history):
    engine=SparseAdjudicator(owner.adjudicator,owner.entity_cache,owner.routing)
    decisions=[];final=[];selections=[];calls=[]
    def child(label,configuration):
        return identity.model_copy(update={'call_id':uuid5(identity.call_id,label),
            'config_sha256':canonical_hash(configuration)})
    ix=child('cascade-index',owner.config.embedding)
    old=owner.previous.get(identity.account_id)
    index=await build_fact_index(tuple(candidates.values()),history_sha256=canonical_hash(history),
        embedding_client=owner.embedding,identity=ix,recorder=owner.recorder,price=owner.config.price,previous=old)
    missing=sum(1 for c in candidates.values() if old is None or not any(
        canonical_hash(f.candidate)==canonical_hash(c) for f in old.facts))
    calls.extend(uuid5(ix.call_id,f'embedding-batch-{s}') for s in range(0,missing,owner.config.embedding.max_batch))
    owner.previous[identity.account_id]=index
    from contexthub.discovery.adjudication import reference_registry
    for target in targets:
        eligible={i:c for i,c in candidates.items() if c.kind!='history_source' or c.sources[0] not in target.sources}
        eligible[target.id]=target
        target_index=index.model_copy(update={'facts':tuple(f for f in index.facts if f.candidate.id in eligible)})
        required=tuple(c.id for c in eligible.values() if c.id!=target.id and any(
            p.proposition_id==target.id and p.source in c.sources for p in history.provenance))
        policy=owner.config.retrieval
        if full_history:policy=policy.model_copy(update={'initial_candidates':max(1,len(eligible)-1)})
        selection=None;next_selection=None;seen_gaps=set();last_results={}
        while True:
            selection=next_selection or select_facts(target_index,policy,target.id,required_ids=required)
            if not selection.selected_ids:selections.append(selection);break
            chosen=tuple(eligible[i] for i in selection.selected_ids)
            sources=set(target.sources)
            for c in chosen:sources.update(c.sources)
            bundle=EvidenceSet(evidence=tuple(e for e in evidence if e.source in sources),
                complete=True,at_max_scope=selection.universe_exhausted,issues=())
            ident=child(f'cascade-{target.id}-{selection.generation}',engine.configuration)
            local_id=uuid5(ident.call_id,'cascade-checks')
            work=LocalOperation(owner.recorder,ident.model_copy(update={'call_id':local_id,
                'model_role':'local','operation':'cascade_checks'}),dict(target=target.model_dump(mode="json"),candidates=[c.model_dump(mode="json") for c in chosen],bundle=bundle.model_dump(mode="json")))
            calls.append(local_id);await work.start();metered=MeteredDB(db,work)
            try:
                mentions=locate(owner.adjudicator.config.stage0,bundle.evidence)
                results,traces,uses,gaps,proposed=await engine.judge(metered,target=target,candidates=chosen,
                    bundle=bundle,identity=ident,generation=selection.generation,mentions=mentions,
                    required_ids=required,work=work)
                calls.extend(t.call_id for t in traces)
                for use in uses:calls.extend(t.call_id for t in use.record.traces)
                round_decisions=[Decision(upstream_id=i,downstream_id=target.id,result=r) for i,r in results.items()]
                decisions.extend(round_decisions);last_results=results
                registry,_=reference_registry(bundle.evidence);audits=[];lead_ids=set();fresh_gaps=[]
                for gap in gaps:
                    status,leads=gap_leads(gap,registry=registry,unexamined=selection.unexamined_ids,
                        candidates=eligible,bundle=bundle,seen=seen_gaps)
                    seen_gaps.add(gap_key(gap))
                    if leads:lead_ids.update(leads);fresh_gaps.append(gap)
                    audits.append(GapAudit(gap=gap,key=gap_key(gap),status=status,lead_ids=leads))
                next_selection=None
                if lead_ids and not selection.universe_exhausted:
                    query='\n'.join(g.missing_fact+' '+ ' '.join(g.search_terms) for g in fresh_gaps)
                    gid=child(f'cascade-gap-{target.id}-{selection.generation}',owner.config.embedding).model_copy(
                        update={'operation':'embed_evidence_gap','model_role':'embedding'})
                    calls.append(gid.call_id)
                    vector=(await owner.embedding.embed_batch((query,),call=RecordedCall(identity=gid,
                        policy=owner.config.embedding.retry,recorder=owner.recorder,price=owner.config.price)))[0]
                    candidate_selection=select_facts(target_index,policy,target.id,required_ids=required,
                        previous=selection,gap_vector=vector)
                    added=set(candidate_selection.selected_ids)-set(selection.selected_ids)
                    new_sources={s for i in added & lead_ids for s in eligible[i].sources}
                    visible_refs={canonical_hash(e) for e in bundle.evidence}
                    gained=any(e.source in new_sources and canonical_hash(e) not in visible_refs for e in evidence)
                    if gained:next_selection=candidate_selection
                    else:audits=[a.model_copy(update={'status':'retrieval_added_no_lead'}) if a.lead_ids else a for a in audits]
                selection=selection.model_copy(update={'cascade':CascadeAudit(traces=traces,proposed_ids=proposed,
                    not_selected_ids=tuple(i for i,r in results.items() if r.action=='not_selected'),
                    gaps=tuple(audits),identities=uses)})
                selections.append(selection)
                await work.finish(dict(selection=selection.model_dump(mode="json"),decisions=[d.model_dump(mode="json") for d in round_decisions]))
            except BaseException as exc:
                await work.finish({'target_id':str(target.id)},error_code(exc));raise
            if next_selection is None:
                final.extend(Decision(upstream_id=i,downstream_id=target.id,result=r) for i,r in last_results.items())
                break
    return decisions,final,[],selections,index,tuple(dict.fromkeys(calls))
