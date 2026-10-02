"""Build an audited immutable fact index from already admitted candidates.

The caller owns history authorization. No unrecorded cache, token estimates,
implicit retries, global cross-account reuse, or source text shortening.
"""
from uuid import uuid5

from contexthub.discovery.semantic_candidates import EmbeddedFact, FactIndexSnapshot
from contexthub.llm.retry_policy import RecordedCall
from contexthub.models.knowledge import ContractError,canonical_hash


async def build_fact_index(candidates, *, history_sha256, embedding_client, identity,
                           recorder, price=None, previous=None):
    cfg=embedding_client.config
    model_binding=canonical_hash(cfg)
    ids=[c.id for c in candidates]
    if len(ids)!=len(set(ids)):
        raise ContractError('duplicate_retrieval_candidate')
    if any(s.account_id!=identity.account_id for c in candidates for s in c.sources):
        raise ContractError('retrieval_tenant_mismatch')
    if any(not c.text for c in candidates):
        raise ContractError('embedding_fact_text_missing')
    # Prior embeddings may only be reused for byte-identical admitted candidates,
    # with the same account and exact embedding configuration. Old history itself
    # does not grant any source access in the new history.
    old={}
    if previous is not None:
        if previous.account_id!=identity.account_id or previous.embedding_identity_sha256!=model_binding:
            raise ContractError('embedding_cache_identity_mismatch')
        old={canonical_hash(f.candidate):f.vector for f in previous.facts}
    missing=[c for c in candidates if canonical_hash(c) not in old]
    for start in range(0,len(missing),cfg.max_batch):
        group=missing[start:start+cfg.max_batch]
        ident=identity.model_copy(update={'call_id':uuid5(identity.call_id,f'embedding-batch-{start}'),
            'config_sha256':model_binding,'operation':'embed_facts','model_role':'embedding'})
        vectors=await embedding_client.embed_batch(tuple(c.text for c in group),call=RecordedCall(
            identity=ident,policy=cfg.retry,recorder=recorder,price=price))
        for c,v in zip(group,vectors): old[canonical_hash(c)]=v
    return FactIndexSnapshot(account_id=identity.account_id,history_sha256=history_sha256,
        embedding_identity_sha256=model_binding,
        facts=tuple(EmbeddedFact(candidate=c,vector=old[canonical_hash(c)]) for c in candidates))
