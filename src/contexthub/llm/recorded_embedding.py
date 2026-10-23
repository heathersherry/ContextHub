"""Explicit embedding transport with the same durable retry owner as chat.

No padding, fallback model, implicit retry, clipping, or inferred provider usage.
"""
import asyncio
from datetime import datetime, timezone
from math import isfinite, fsum
from pathlib import Path
import time
from urllib.parse import urlsplit

import httpx
from pydantic import Field, model_validator

from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.llm.response_archive import archive_response
from contexthub.llm.retry_policy import RecordedCall, RetryPolicy, ResponseSchemaError
from contexthub.models.execution import AttemptRecord, AttemptError, Usage, calculate_cost
from contexthub.models.knowledge import Contract, ContractError, Name, canonical_hash


class EmbeddingConfig(Contract):
    model: Name
    base_url: Name
    dimensions: int = Field(strict=True, ge=1)
    send_dimensions: bool
    max_batch: int = Field(strict=True, ge=1)
    response_archive_directory: Name
    retry: RetryPolicy

    @model_validator(mode='after')
    def safe(self):
        u=urlsplit(self.base_url)
        if u.scheme not in ('http','https') or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ContractError('embedding_endpoint_must_be_secret_free')
        if not Path(self.response_archive_directory).is_absolute():
            raise ContractError('response_archive_absolute_path_required')
        return self


class RecordedEmbeddingClient:
    def __init__(self, config: EmbeddingConfig, *, api_key: str, transport=None):
        self.config=config
        self._api_key=api_key
        self._client=httpx.AsyncClient(base_url=config.base_url.rstrip('/'),
            headers={'Authorization':f'Bearer {api_key}'},transport=transport,
            timeout=config.retry.timeout_seconds)

    async def close(self):
        await self._client.aclose()

    def _parse(self,data,count):
        if not isinstance(data,dict) or not isinstance(data.get('data'),list) or len(data['data'])!=count:
            raise ResponseSchemaError('embedding_count_mismatch')
        vectors={}
        for row in data['data']:
            if not isinstance(row,dict): raise ResponseSchemaError('embedding_row_invalid')
            i=row.get('index'); v=row.get('embedding')
            if type(i) is not int or not 0<=i<count or i in vectors:
                raise ResponseSchemaError('embedding_index_invalid')
            if (not isinstance(v,list) or len(v)!=self.config.dimensions
                or any(type(x) not in (int,float) or not isfinite(x) for x in v)):
                raise ResponseSchemaError('embedding_vector_invalid')
            try: norm=fsum(x*x for x in v)
            except OverflowError: raise ResponseSchemaError('embedding_vector_invalid') from None
            if not isfinite(norm) or norm<=0: raise ResponseSchemaError('embedding_vector_invalid')
            vectors[i]=tuple(float(x) for x in v)
        return tuple(vectors[i] for i in range(count))

    async def embed_batch(self,texts: tuple[str,...], *, call: RecordedCall):
        if not texts or len(texts)>self.config.max_batch or any(not isinstance(t,str) or not t for t in texts):
            raise ContractError('invalid_embedding_batch')
        if call.policy != self.config.retry or call.identity.config_sha256 != canonical_hash(self.config):
            raise ContractError('embedding_call_config_mismatch')
        payload={'model':self.config.model,'input':list(texts),'encoding_format':'float'}
        if self.config.send_dimensions: payload['dimensions']=self.config.dimensions
        for number in range(1,call.policy.max_attempts+1):
            started=datetime.now(timezone.utc)
            usage=Usage(completeness='missing',missing_reason='request_not_completed')
            common=dict(identity=call.identity,attempt_no=number,requested_model=self.config.model,
                input_sha256=canonical_hash(payload),prompt_sha256=canonical_hash({'input':texts}),
                request_config_sha256=canonical_hash(self.config),retry_policy_sha256=canonical_hash(call.policy),
                started_at=started,price=call.price)
            await call.recorder.append(AttemptRecord(**common,phase='started',outcome='started',usage=usage,
                actual_model_missing_reason='request_not_completed',request_id_missing_reason='request_not_completed',
                request_payload=payload if call.capture_raw_output else None,
                cost=calculate_cost(usage,call.price,None)))
            tick=time.monotonic(); response=None; data=None; error=None; archived={}; vectors=None
            try:
                timeout = call.policy.timeout_for_attempt(number)
                async with asyncio.timeout(timeout):
                    response=await self._client.post('/embeddings',json=payload,timeout=timeout)
                try: data=response.json()
                except ValueError: pass
                archived=archive_response(self.config.response_archive_directory,identity=call.identity,
                    attempt_no=number,request=payload,response=response,api_key=self._api_key)
                response.raise_for_status()
                vectors=self._parse(data,len(texts))
            except (Exception,asyncio.CancelledError) as exc:
                error=exc
            # Embedding APIs often omit completion_tokens. Preserve prompt/total
            # as partial usage rather than inventing a returned zero or a price.
            usage=OpenAIChatClient._response_usage(data)
            model=data.get('model') if isinstance(data,dict) else None
            model=model if isinstance(model,str) and model.strip() else None
            request_id=response.headers.get('x-request-id') if response is not None else None
            request_id=request_id if request_id and request_id.strip() else None
            retryable=error is not None and call.policy.is_retryable(error)
            again=retryable and number<call.policy.max_attempts
            cancelled=isinstance(error,asyncio.CancelledError)
            stop='success' if error is None else 'cancelled' if cancelled else 'retry_scheduled' if again else 'retry_exhausted' if retryable else 'non_retryable'
            await call.recorder.append(AttemptRecord(**common,**archived,phase='finished',
                outcome='success' if error is None else 'cancelled' if cancelled else 'error',
                actual_model=model,actual_model_missing_reason=None if model else 'provider_not_returned',
                request_id=request_id,request_id_missing_reason=None if request_id else 'provider_not_returned',
                output_sha256=canonical_hash({'vectors':vectors}) if vectors is not None else None,
                finished_at=datetime.now(timezone.utc),wall_seconds=time.monotonic()-tick,
                status_code=response.status_code if response is not None else None,
                error=AttemptError(code='http_'+str(response.status_code) if isinstance(error,httpx.HTTPStatusError) else type(error).__name__,
                    error_class=type(error).__name__,retryable=retryable) if error else None,
                stop_reason=stop,usage=usage,cost=calculate_cost(usage,call.price,model)))
            if error is None: return vectors
            if not again: raise error
            await call.sleep(call.policy.delay(number,error))
        raise RuntimeError('unreachable')
