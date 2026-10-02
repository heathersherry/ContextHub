"""Opt-in target-group adjudication; shared checks, distinct physical call ledger.

Not selected by the legacy discovery loop. Every physical call has one ledger
identity; item traces may reference that shared call but must not multiply cost.
"""
import json
from uuid import uuid5

from contexthub.discovery.adjudication import (
    Adjudication, JudgmentTrace, reference_registry, validate_judgment,
)
from contexthub.discovery.dependency_batch import BATCH_INSTRUCTIONS, batch_payload, parse_batch, compact_batch_judgment, candidate_number_map
from contexthub.discovery._work import LocalOperation, MeteredDB
from contexthub.discovery.extraction import error_code
from contexthub.discovery.planner import choose_price
from contexthub.llm.retry_policy import RecordedCall
from contexthub.models.knowledge import ContractError,canonical_hash


class BatchAdjudicator:
    def __init__(self, adjudicator):
        self.base=adjudicator
        self.configuration={'version':'dependency-batch-v2',
            'prompt':BATCH_INSTRUCTIONS,'adjudication':adjudicator.config.model_dump(mode='json')}

    async def judge(self,db,*,target,candidates,bundle,identity,generation=0,required_ids=(),
                    identity_resolutions=None):
        if identity.config_sha256!=canonical_hash(self.configuration):
            raise ContractError('batch_adjudication_config_mismatch')
        # Validate candidate IDs even on the no-call/incomplete path.
        batch_payload(target,candidates,bundle,role='normal')
        ids=tuple(c.id for c in candidates)
        numbers=candidate_number_map(ids)
        if not set(required_ids)<=set(ids): raise ContractError('unknown_batch_provenance')
        references=identity_resolutions or {}
        if not set(references)<=set(ids): raise ContractError('unknown_identity_candidate')
        digest=canonical_hash(bundle); registry,reverse=reference_registry(bundle.evidence)
        for resolutions in references.values():
            for ref in resolutions:
                r=ref.result
                if (r.kind!='entity' or r.evidence_sha256!=digest or r.generation!=generation
                    or not ((r.action=='complete' and r.verdict in ('SAME','DIFFERENT'))
                            or (r.action=='pending' and r.verdict=='UNKNOWN'))):
                    raise ContractError('identity_resolution_not_current_complete')
                for mention in ref.mentions:
                    if not any(mention.evidence.source==e.source and e.start<=mention.evidence.start<mention.evidence.end<=e.end for e in bundle.evidence):
                        raise ContractError('identity_resolution_outside_evidence')
        work=LocalOperation(self.base.recorder,identity.model_copy(update={
            'call_id':uuid5(identity.call_id,'batch-checks'),'model_role':'local','operation':'batch_checks'}),
            {'target':target.model_dump(mode='json'),'candidates':[c.model_dump(mode='json') for c in candidates],
             'bundle':bundle.model_dump(mode='json'),'identity_resolutions':{str(i):[r.model_dump(mode='json') for r in rs] for i,rs in references.items()}})
        await work.start(); db=MeteredDB(db,work)
        traces={i:[] for i in ids}; output={}

        def finish(i,verdict,action,reason,accepted=None,plan=None):
            output[i]=Adjudication(kind='dependency',verdict=verdict,action=action,reason=reason,
                evidence_sha256=digest,traces=tuple(traces[i]),accepted=accepted,
                plan_id=plan.id if plan else None,generation=generation)

        def missing(i):
            finish(i,'UNKNOWN','pending' if bundle.at_max_scope else 'read_more',
                'missing_premise_at_max' if bundle.at_max_scope else 'missing_evidence')

        async def invoke(role,items):
            strong=role=='strong'; cfg=self.base.config.strong if strong else self.base.config.weak
            payload=batch_payload(target,items,bundle,role='normal' if strong else role,candidate_numbers=numbers)
            payload['role']=role
            payload['explicit_provenance_candidate_numbers']=[numbers[c.id] for c in items if c.id in required_ids]
            payload['identity_resolutions']={str(numbers[c.id]):[
                {'mentions':[{'text':m.text,'evidence_ids':[reverse[canonical_hash(e)] for e in bundle.evidence if m.evidence.source==e.source and e.start<=m.evidence.start<m.evidence.end<=e.end]} for m in r.mentions],
                 'verdict':r.result.verdict,'reason':r.result.reason,
                 'accepted':compact_batch_judgment(r.result.accepted,reverse)
                    if r.result.accepted is not None and r.result.verdict!='UNKNOWN' else None,
                 'failure_notes':[dict(role=t.role,errors=list(t.errors)) for t in r.result.traces]}
                for r in references.get(c.id,())] for c in items}
            if strong:
                payload['prior_judgments']={str(numbers[c.id]):[
                    dict(role=t.role,errors=list(t.errors),judgment=compact_batch_judgment(t.judgment,reverse)
                         if t.judgment is not None else None) for t in traces[c.id]] for c in items}
            call_id=uuid5(identity.call_id,role)
            ident=identity.model_copy(update={'call_id':call_id,'model_role':role,'operation':'judge_dependency_batch'})
            item_ids=tuple(c.id for c in items)
            # Parse failures need partial-item salvage, so validation happens after
            # the physical call. Raw response is still archived by the chat client.
            raw=None; failure=None
            try:
                raw=await self.base.clients['strong' if strong else 'weak'].complete(
                    BATCH_INSTRUCTIONS+'\nInput:\n'+json.dumps(payload,ensure_ascii=False),
                    max_tokens=cfg.max_tokens,call=RecordedCall(identity=ident,policy=cfg.retry,
                        recorder=self.base.recorder,price=self.base.config.strong_price if strong else self.base.config.weak_price))
            except Exception as exc:
                failure=error_code(exc)
            parsed=parse_batch(raw,item_ids,bundle,candidate_numbers=numbers) if raw is not None else None
            by_id={i.candidate_id:i for i in parsed.items} if parsed else {}
            for c in items:
                item=by_id.get(c.id); judgment=item.judgment if item else None
                errors=list(item.errors) if item else [failure or 'batch_call_failed']
                if parsed and parsed.errors:
                    errors.extend(parsed.errors)
                if judgment is not None:
                    try:
                        errors.extend(await validate_judgment(db,judgment,kind='dependency',bundle=bundle,
                            evidence_service=self.base.evidence,work=work,explicit_provenance=c.id in required_ids))
                    except Exception as exc: errors.append(error_code(exc))
                traces[c.id].append(JudgmentTrace(role=role,call_id=call_id,payload_sha256=canonical_hash(payload),
                    evidence_sha256=digest,judgment=judgment,errors=tuple(errors)))

        try:
            if not bundle.complete or bundle.issues:
                for i in ids: finish(i,'UNKNOWN','pending' if bundle.at_max_scope else 'read_more','search_incomplete')
            else:
                for ref in bundle.evidence:
                    await self.base.evidence.resolve(db,ref)
                    work.add('hard_checks')
                await invoke('normal',candidates)
                active=[]
                for c in candidates:
                    t=traces[c.id][-1]
                    if t.judgment is None: finish(c.id,'UNKNOWN','pending','weak_call_failed')
                    elif t.judgment.missing: missing(c.id)
                    else: active.append(c)
                if active: await invoke('challenge',tuple(active))
                strong=[]; plans={}
                for c in active:
                    first,second=traces[c.id]
                    if second.judgment is None: finish(c.id,'UNKNOWN','pending','weak_call_failed');continue
                    if second.judgment.missing: missing(c.id);continue
                    accepted=not first.errors and not second.errors and first.judgment.verdict==second.judgment.verdict!='UNKNOWN'
                    plan=choose_price(self.base.config.plans,self.base.config.lambda_p1) if accepted else None
                    if accepted and plan is None: finish(c.id,'UNKNOWN','pending','no_legal_complete_plan');continue
                    if accepted and not plan.optional_strong:
                        finish(c.id,first.judgment.verdict,'complete','dual_weak',first.judgment,plan)
                    else: strong.append(c);plans[c.id]=plan
                if strong: await invoke('strong',tuple(strong))
                for c in strong:
                    t=traces[c.id][-1];plan=plans[c.id]
                    if t.judgment and t.judgment.missing: missing(c.id)
                    elif t.errors or t.judgment is None or t.judgment.verdict=='UNKNOWN':
                        finish(c.id,'UNKNOWN','pending','strong_unresolved',plan=plan)
                    else:
                        plan=plan or choose_price(self.base.config.plans,self.base.config.lambda_p1)
                        if plan is None: finish(c.id,'UNKNOWN','pending','no_legal_complete_plan')
                        else: finish(c.id,t.judgment.verdict,'complete',
                            'optional_strong' if plans[c.id] else 'mandatory_strong',t.judgment,plan)
            await work.finish({str(i):r.model_dump(mode='json') for i,r in output.items()})
            return output
        except BaseException as exc:
            await work.finish({'completed_ids':[str(i) for i in output]},error_code(exc))
            raise
