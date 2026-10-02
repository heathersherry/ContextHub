"""The sole application/experiment composition entry point."""

from contextlib import AsyncExitStack
from dataclasses import dataclass
from contexthub.knowledge_config import KnowledgeConfig
from contexthub.models.knowledge import ContractError
from contexthub.services.execution_ledger import ExecutionLedger
from contexthub.discovery.extraction import ExtractionService
from contexthub.discovery.adjudication import AdjudicationService
from contexthub.discovery.search import SearchService
from contexthub.discovery.service import DiscoveryService
from contexthub.propagation.executor import MaintenanceExecutor
from contexthub.propagation.hard_checks import HardChecks
from contexthub.propagation.candidate import CandidateGenerator
from contexthub.propagation.verifier import FixedVerifier
from contexthub.propagation.profiles import MaintenanceRunner


@dataclass(repr=False)
class KnowledgeSecrets:
    extraction: str
    adjudication: str
    base: str
    strong: str
    verifier: str
    change_location: str
    embedding: str | None = None


async def create_knowledge_service(
    repository, config, secrets, *, context_service, rules=(), transport=None
):
    # Validate EVERYTHING before constructing the first external client.
    config = KnowledgeConfig.model_validate(config.model_dump(mode="json"))
    if tuple((r.id, r.version) for r in rules) != tuple(
        (r.id, r.version) for r in config.rules
    ):
        raise ContractError("rule_registry_mismatch")
    if not isinstance(secrets, KnowledgeSecrets) or any(
        not getattr(secrets, role) for role in KnowledgeSecrets.__annotations__ if role != "embedding"
    ):
        raise ContractError("knowledge_credentials_missing")
    if config.discovery.semantic is not None and not secrets.embedding:
        raise ContractError("embedding_credentials_missing")
    from contexthub.services.change_location import ChangeLocator
    from contexthub.services.knowledge_service import KnowledgeService

    stack = AsyncExitStack()
    await stack.__aenter__()
    ledger = ExecutionLedger(repository)
    try:

        def own(client):
            stack.push_async_callback(client.close)
            return client

        extractor = own(
            ExtractionService(
                config.extraction,
                api_key=secrets.extraction,
                recorder=ledger,
                transport=transport,
                price=config.extraction_price,
            )
        )
        from contexthub.llm.chat_client import OpenAIChatClient

        adjudication_clients = {}
        for role in ("weak", "strong"):
            cfg = getattr(config.discovery.adjudication, role)
            adjudication_clients[role] = own(
                OpenAIChatClient(
                    secrets.adjudication,
                    model=cfg.model,
                    base_url=cfg.base_url,
                    temperature=cfg.temperature,
                    request_parameters=cfg.request_parameters,
                    timeout=cfg.retry.timeout_seconds,
                    transport=transport,
                )
            )
        adjudicator = AdjudicationService(
            config.discovery.adjudication,
            api_key=secrets.adjudication,
            recorder=ledger,
            clients=adjudication_clients,
        )
        semantic_runner = None
        if config.discovery.semantic is not None:
            from contexthub.llm.recorded_embedding import RecordedEmbeddingClient
            from contexthub.discovery.semantic_runner import SemanticRunner
            embedding = own(RecordedEmbeddingClient(config.discovery.semantic.embedding, api_key=secrets.embedding, transport=transport))
            semantic_runner = SemanticRunner(config.discovery.semantic, embedding=embedding, adjudicator=adjudicator, recorder=ledger, routing=config.discovery.identity_routing)
        discovery = DiscoveryService(
            config.discovery,
            search=SearchService(recorder=ledger),
            semantic_runner=semantic_runner,
            adjudicator=adjudicator,
            recorder=ledger,
        )
        executor = MaintenanceExecutor(repository, recorder=ledger)
        base = own(
            CandidateGenerator(
                config.base, api_key=secrets.base, recorder=ledger, transport=transport
            )
        )
        strong = own(
            CandidateGenerator(
                config.strong,
                api_key=secrets.strong,
                recorder=ledger,
                transport=transport,
            )
        )
        verifier = own(
            FixedVerifier(
                config.verifier,
                checks=HardChecks(executor, rules),
                api_key=secrets.verifier,
                recorder=ledger,
                transport=transport,
            )
        )
        runner = MaintenanceRunner(
            executor,
            config.profile,
            base=base,
            strong=strong,
            verifier=verifier,
            rules=rules,
        )
        locator = own(
            ChangeLocator(
                config.change_location,
                api_key=secrets.change_location,
                recorder=ledger,
                transport=transport,
            )
        )
        return KnowledgeService(
            repository,
            config,
            extractor,
            discovery,
            executor,
            runner,
            locator,
            context_service,
            stack,
        )
    except BaseException:
        await stack.aclose()
        raise
