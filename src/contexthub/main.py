"""Application entry point."""

from contextlib import asynccontextmanager, AsyncExitStack
import os
from dataclasses import dataclass

import asyncpg
from fastapi import FastAPI

from contexthub.api.middleware import AuthMiddleware
from contexthub.api.routers.contexts import router as contexts_router
from contexthub.api.routers.memories import router as memories_router
from contexthub.api.routers.feedback import router as feedback_router
from contexthub.api.routers.documents import router as documents_router
from contexthub.api.routers.enforce import router as enforce_router
from contexthub.api.routers.search import router as search_router
from contexthub.api.routers.skills import router as skills_router
from contexthub.api.routers.tools import router as tools_router
from contexthub.config import Settings
from contexthub.memory.factory import MemoryConfig, create_memory_engine
from contexthub.db.codecs import init_pg_connection
from contexthub.db.repository import PgRepository
from contexthub.enforcement.guardrails.closure import ClosureGuardrail
from contexthub.enforcement.guardrails.flow import FlowGuardrail
from contexthub.enforcement.guardrails.handoff import HandoffGuardrail
from contexthub.enforcement.guardrails.tool_state import ToolStateGuardrail
from contexthub.enforcement.service import EnforcementService
from contexthub.enforcement.staleness import StalenessChecker
from contexthub.generation.base import ContentGenerator
from contexthub.llm.factory import create_chat_client, create_embedding_client
from contexthub.retrieval.long_doc import (
    KeywordRetriever,
    LongDocRetrievalCoordinator,
    TreeRetriever,
)
from contexthub.retrieval.router import RetrievalRouter
from contexthub.services.acl_service import ACLService
from contexthub.services.audit_service import AuditService
from contexthub.services.context_service import ContextService
from contexthub.services.feedback_service import FeedbackService
from contexthub.services.indexer_service import IndexerService
from contexthub.services.lifecycle_scheduler import LifecycleScheduler
from contexthub.services.lifecycle_service import LifecycleService
from contexthub.services.memory_service import MemoryService
from contexthub.services.retrieval_service import RetrievalService
from contexthub.services.skill_service import SkillService
from contexthub.services.document_ingester import LongDocumentIngester
from contexthub.store.context_store import ContextStore
from contexthub.propagation.registry import PropagationRuleRegistry
from contexthub.services.propagation_engine import PropagationEngine
from contexthub.connectors.mock_connector import MockCatalogConnector
from contexthub.generation.table_schema import TableSchemaGenerator
from contexthub.services.catalog_sync_service import CatalogSyncService
from contexthub.services.masking_service import MaskingService
from contexthub.services.reconciler_service import ReconcilerService
from contexthub.services.share_service import ShareService
from contexthub.api.routers.datalake import router as datalake_router
from contexthub.api.routers.admin import router as admin_router


@dataclass
class ApplicationDependencies:
    """Explicit local/test boundaries; default callables preserve old application behavior."""
    embedding_factory: object = None
    chat_factory: object = None
    memory_transport: object = None
    memory_chat_key: str | None = None
    memory_embedding_key: str | None = None
    memory_jev_key: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = getattr(app.state, "bootstrap_settings", None) or Settings()
    dependencies = getattr(app.state, "bootstrap_dependencies", None) or ApplicationDependencies()
    memory_config = None
    if settings.memory_enabled:
        # Validate configuration before starting clients or background services.
        try:
            memory_config = MemoryConfig.load(settings.memory_config_path)
            memory_chat_key = dependencies.memory_chat_key or os.environ.get("MEMORY_CHAT_API_KEY")
            memory_embedding_key = dependencies.memory_embedding_key or os.environ.get("MEMORY_EMBEDDING_API_KEY")
            if not memory_chat_key or not memory_embedding_key:
                raise ValueError("memory credentials missing")
            memory_jev_key = dependencies.memory_jev_key or os.environ.get("TYPESAFE_API_KEY")
            if memory_config.uses_jev and not memory_jev_key:
                raise ValueError("memory Jev credential missing")
        except Exception:
            raise RuntimeError("memory_configuration_invalid") from None
    stack = AsyncExitStack()
    await stack.__aenter__()
    pool = await asyncpg.create_pool(
        dsn=settings.asyncpg_database_url,
        min_size=2,
        max_size=10,
        init=init_pg_connection,
    )
    stack.push_async_callback(pool.close)
    embedding_client = None
    chat_client = None
    lifecycle_scheduler = None
    propagation_engine = None

    try:
        repo = PgRepository(pool)

        acl_service = ACLService()
        masking_service = MaskingService()
        audit_service = AuditService(pool=pool)
        share_service = ShareService(acl_service, audit=audit_service)
        staleness_checker = StalenessChecker()
        enforcement_service = EnforcementService(
            [
                HandoffGuardrail(acl=acl_service, staleness=staleness_checker),
                ClosureGuardrail(staleness=staleness_checker),
                ToolStateGuardrail(staleness=staleness_checker),
                FlowGuardrail(acl=acl_service),
            ],
            audit=audit_service,
        )

        # Task 3 services
        embedding_client = (dependencies.embedding_factory or create_embedding_client)(settings)
        if hasattr(embedding_client, "close"):
            stack.push_async_callback(embedding_client.close)
        chat_client = (dependencies.chat_factory or create_chat_client)(settings)
        if hasattr(chat_client, "close"):
            stack.push_async_callback(chat_client.close)
        content_generator = ContentGenerator()
        indexer_service = IndexerService(
            content_generator,
            embedding_client,
            embedding_dimensions=settings.embedding_dimensions,
        )
        lifecycle_service = LifecycleService(audit=audit_service, indexer=indexer_service)
        context_store = ContextStore(
            acl_service,
            masking_service,
            audit=audit_service,
            lifecycle=lifecycle_service,
        )

        # Inject indexer into ContextService for embedding consistency
        context_service = ContextService(context_store, acl_service, indexer_service, audit=audit_service)
        memory_engine = None
        if memory_config is not None:
            memory_engine = await create_memory_engine(
                settings.asyncpg_database_url, memory_config,
                chat_key=memory_chat_key, embedding_key=memory_embedding_key,
                jev_key=memory_jev_key,
                transport=dependencies.memory_transport,
            )
            stack.push_async_callback(memory_engine.close)
        app.state.memory_engine = memory_engine
        memory_service = MemoryService(indexer_service, acl_service, masking_service,
                                       audit=audit_service)
        skill_service = SkillService(indexer_service, acl_service, masking_service, audit=audit_service)

        # Task 4: retrieval
        retrieval_router = RetrievalRouter.default()
        long_doc_coordinator = LongDocRetrievalCoordinator()
        long_doc_coordinator.register_strategy("tree", TreeRetriever(chat_client))
        long_doc_coordinator.register_strategy("keyword", KeywordRetriever(chat_client))
        retrieval_service = RetrievalService(
            retrieval_router, embedding_client, acl_service,
            masking_service=masking_service,
            audit_service=audit_service,
            long_doc_coordinator=long_doc_coordinator,
            over_retrieve_factor=settings.search_over_retrieve_factor,
        )
        document_ingester = LongDocumentIngester(
            chat_client=chat_client,
            embedding_client=embedding_client,
            content_generator=content_generator,
            acl=acl_service,
            audit=audit_service,
            doc_store_root=settings.doc_store_root,
            max_document_size_mb=settings.max_document_size_mb,
            max_token_per_node=settings.max_token_per_node,
        )
        feedback_service = FeedbackService(acl_service, audit=audit_service)

        app.state.settings = settings
        app.state.repo = repo
        app.state.acl_service = acl_service
        app.state.context_store = context_store
        app.state.context_service = context_service
        app.state.memory_service = memory_service
        app.state.skill_service = skill_service
        app.state.indexer_service = indexer_service
        app.state.lifecycle_service = lifecycle_service
        app.state.retrieval_service = retrieval_service
        app.state.long_doc_retrieval_coordinator = long_doc_coordinator
        app.state.masking_service = masking_service
        app.state.embedding_client = embedding_client
        app.state.chat_client = chat_client
        app.state.audit_service = audit_service
        app.state.share_service = share_service
        app.state.feedback_service = feedback_service
        app.state.document_ingester = document_ingester
        app.state.enforcement_service = enforcement_service

        # Task 7: Carrier-specific services
        catalog_connector = MockCatalogConnector()
        table_schema_generator = TableSchemaGenerator()
        catalog_sync_service = CatalogSyncService(
            connector=catalog_connector,
            indexer=indexer_service,
            table_schema_generator=table_schema_generator,
        )
        reconciler_service = ReconcilerService(repo=repo, indexer=indexer_service)

        app.state.catalog_sync_service = catalog_sync_service
        app.state.reconciler_service = reconciler_service

        lifecycle_scheduler = LifecycleScheduler(
            lifecycle=lifecycle_service,
            repo=repo,
            pool=pool,
            interval_seconds=settings.lifecycle_sweep_interval,
        )
        app.state.lifecycle_scheduler = lifecycle_scheduler

        # Task 5: PropagationEngine
        rule_registry = PropagationRuleRegistry.default()
        propagation_engine = PropagationEngine(
            repo=repo,
            pool=pool,
            dsn=settings.asyncpg_database_url,
            rule_registry=rule_registry,
            lifecycle=lifecycle_service,
            sweep_interval=settings.propagation_sweep_interval,
            lease_timeout=settings.propagation_lease_timeout,
        )

        app.state.propagation_engine = propagation_engine
        if settings.propagation_enabled:
            # start() cleans its own partial initialization on failure.
            await propagation_engine.start()
            stack.push_async_callback(propagation_engine.stop)
        if settings.lifecycle_enabled:
            stack.push_async_callback(lifecycle_scheduler.stop)
            await lifecycle_scheduler.start()

        yield
    finally:
        await stack.aclose()


def create_app(settings: Settings | None = None, *, dependencies: ApplicationDependencies | None = None):
    application = FastAPI(title="ContextHub", lifespan=lifespan)
    if settings is not None:
        application.state.bootstrap_settings = settings
    if dependencies is not None:
        application.state.bootstrap_dependencies = dependencies
    application.add_middleware(AuthMiddleware)
    for router in (contexts_router, memories_router, feedback_router, documents_router,
                   enforce_router, skills_router, search_router, tools_router,
                   datalake_router, admin_router):
        application.include_router(router)

    @application.get("/health")
    async def health():
        return {"status": "ok"}

    return application


app = create_app()
