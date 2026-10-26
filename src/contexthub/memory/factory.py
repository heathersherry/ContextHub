"""The same memory assembly for the service and benchmark adapters."""

from contextlib import AsyncExitStack
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.llm.recorded_embedding import EmbeddingConfig, RecordedEmbeddingClient
from contexthub.llm.request_parameters import RequestParameters
from contexthub.llm.retry_policy import RetryPolicy
from .condition_binder import ConditionBinder
from .engine import MemoryEngine
from .extractor import Extractor
from .indexer import Indexer
from .maintenance_executor import MaintenanceExecutor
from .pipeline import MemoryPipeline
from .retriever import Retriever
from .schemas import IndexConfig, RetrievalConfig
from .storage import MemoryStore


def assemble_memory_engine(
    store,
    chat,
    embedding,
    *,
    retrieval,
    retry,
    code_version,
    chat_price=None,
    embedding_price=None,
    binder_factory=None,
    rule_gate=None,
    intent_classifier=None,
):
    indexer = Indexer(store, embedding, price=embedding_price)
    retriever = Retriever(store, indexer, retrieval)
    binder = (
        binder_factory(retriever)
        if binder_factory
        else ConditionBinder(store, chat, retriever, retry=retry, price=chat_price)
    )
    return MemoryEngine(
        MemoryPipeline(
            store,
            Extractor(chat, retry=retry, price=chat_price, rule_gate=rule_gate),
            retriever,
            binder,
            MaintenanceExecutor(
                store,
                retriever,
                indexer,
                chat,
                retry=retry,
                price=chat_price,
                intent_classifier=intent_classifier,
            ),
            code_version=code_version,
        )
    )


class MemoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: IndexConfig
    retrieval: RetrievalConfig
    chat_model: str = Field(min_length=1)
    chat_base_url: str = Field(min_length=1)
    request_parameters: RequestParameters
    retry: RetryPolicy
    embedding: EmbeddingConfig
    code_version: str = Field(min_length=1)
    initialize: bool = False
    rule_gate_backend: Literal["llm", "jev"] = "llm"
    p1_backend: Literal["llm", "jev"] = "llm"
    p2_direct_backend: Literal["llm", "jev"] = "llm"
    p1_jev_threshold: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)
    jev_retry: RetryPolicy | None = None

    @classmethod
    def load(cls, path):
        return cls.model_validate_json(Path(path).read_text())

    @model_validator(mode="after")
    def check(self):
        if (
            self.index.embedding_model != self.embedding.model
            or self.index.dimensions != self.embedding.dimensions
        ):
            raise ValueError("memory embedding and index configurations differ")
        if self.uses_jev and self.jev_retry is None:
            raise ValueError("jev_retry must be explicit when Jev is enabled")
        return self

    @property
    def uses_jev(self):
        return "jev" in (
            self.rule_gate_backend,
            self.p1_backend,
            self.p2_direct_backend,
        )


async def create_memory_engine(
    dsn, config, *, chat_key, embedding_key, jev_key=None, transport=None
):
    stack = AsyncExitStack()
    try:
        store = await MemoryStore.open(dsn, config.index, initialize=config.initialize)
        stack.push_async_callback(store.close)
        chat = OpenAIChatClient(
            chat_key,
            base_url=config.chat_base_url,
            model=config.chat_model,
            temperature=None,
            request_parameters=config.request_parameters,
            transport=transport,
        )
        stack.push_async_callback(chat.close)
        embedding = RecordedEmbeddingClient(
            config.embedding, api_key=embedding_key, transport=transport
        )
        stack.push_async_callback(embedding.close)
        jev = None
        if config.uses_jev:
            if not jev_key:
                raise ValueError("memory Jev credential missing")
            from contexthub.llm.jev_client import JevClient

            jev = JevClient(jev_key, transport=transport)
            stack.push_async_callback(jev.close)
        from .jev_condition_binder import JevConditionBinder
        from .jev_intent_classifier import JevIntentClassifier
        from .jev_rule_gate import JevRuleGate

        engine = assemble_memory_engine(
            store,
            chat,
            embedding,
            retrieval=config.retrieval,
            retry=config.retry,
            code_version=config.code_version,
            binder_factory=(
                lambda retriever: JevConditionBinder(
                    store,
                    jev,
                    retriever,
                    retry=config.jev_retry,
                    threshold=config.p1_jev_threshold,
                )
            )
            if config.p1_backend == "jev"
            else None,
            intent_classifier=JevIntentClassifier(jev, retry=config.jev_retry)
            if config.p2_direct_backend == "jev"
            else None,
            rule_gate=JevRuleGate(jev, retry=config.jev_retry)
            if config.rule_gate_backend == "jev"
            else None,
        )
        engine._resources = stack.pop_all()
        return engine
    except BaseException:
        await stack.aclose()
        raise
