import pytest
from pydantic import ValidationError
from contexthub.knowledge_config import KnowledgeConfig
from contexthub.services.knowledge_factory import create_knowledge_service
from contexthub.models.knowledge import ContractError, canonical_hash
from s7_helpers import configuration, secrets, SyntheticModels


def test_no_defaults_or_privileged_config():
    with pytest.raises(ValidationError):
        KnowledgeConfig.model_validate({})
    for key in ("gold", "task_type", "hop", "question", "path_labels"):
        with pytest.raises(ValidationError):
            KnowledgeConfig.model_validate(
                {**configuration().model_dump(), key: "forbidden"}
            )
    assert "synthetic-not-a-secret" not in configuration().model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["local/base-only", "full/strong-only", "adaptive"])
async def test_factory_shared_models_and_close(profile):
    model = SyntheticModels()
    service = await create_knowledge_service(
        None,
        configuration(profile),
        secrets(),
        context_service=None,
        transport=model.transport,
    )
    assert service.runner.base.config == configuration().base
    assert service.runner.strong.config == configuration().strong
    assert service.runner.verifier.config == configuration().verifier
    assert canonical_hash(service.config) == service.config_hash
    assert model.calls == []
    await service.close()
    assert service.runner.base.client._client.is_closed
    assert service.runner.verifier.client._client.is_closed


@pytest.mark.asyncio
async def test_missing_secret_zero_construction(monkeypatch):
    def no_client(*a, **k):
        raise AssertionError("client constructed")

    monkeypatch.setattr(
        "contexthub.services.knowledge_factory.ExtractionService", no_client
    )
    values = secrets()
    values.verifier = ""
    with pytest.raises(ContractError, match="knowledge_credentials_missing"):
        await create_knowledge_service(
            None, configuration(), values, context_service=None
        )


@pytest.mark.asyncio
async def test_partial_factory_failure_closes_prior_clients(monkeypatch):
    owned = []
    from contexthub.services import knowledge_factory as factory

    original = factory.CandidateGenerator

    def construct(config, **kw):
        if config.model == "strong":
            raise RuntimeError("synthetic initialization failure")
        obj = original(config, **kw)
        owned.append(obj)
        return obj

    monkeypatch.setattr(factory, "CandidateGenerator", construct)
    with pytest.raises(RuntimeError):
        await factory.create_knowledge_service(
            None,
            configuration(),
            secrets(),
            context_service=None,
            transport=SyntheticModels().transport,
        )
    assert owned[0].client._client.is_closed


@pytest.mark.asyncio
async def test_semantic_factory_explicit_secret_and_lifecycle(tmp_path):
    from contexthub.discovery.semantic_runner import SemanticConfig
    from contexthub.discovery.semantic_candidates import RetrievalPolicy
    from contexthub.llm.recorded_embedding import EmbeddingConfig
    from contexthub.discovery.stage0 import Stage0Config
    from knowledge_helpers import policy
    from test_identity_routing import ner,ROUTING
    cfg=configuration()
    stage=Stage0Config(version='names',rules=(),aliases=(),alias_version='empty',
        mention_patterns=(),mention_view='dialogue-content-v2',spacy_ner=ner())
    semantic=SemanticConfig(embedding=EmbeddingConfig(model='fixture',base_url='https://fixture.invalid',
        dimensions=2,send_dimensions=True,max_batch=8,response_archive_directory=str(tmp_path),retry=policy()),
        retrieval=RetrievalPolicy(initial_candidates=8,expansion_factor=2,batch_size=8,max_partial_rounds=3),price=None)
    cfg=cfg.model_copy(update={'discovery':cfg.discovery.model_copy(update={
        'adjudication':cfg.discovery.adjudication.model_copy(update={'stage0':stage}),
        'identity_routing':ROUTING.model_copy(update={'unknown_policy':'advisory'}),'semantic':semantic})})
    missing=secrets();missing.embedding=None
    with pytest.raises(ContractError,match='embedding_credentials_missing'):
        await create_knowledge_service(None,cfg,missing,context_service=None,transport=SyntheticModels().transport)
    values=secrets();values.embedding='synthetic-embedding-secret'
    service=await create_knowledge_service(None,cfg,values,context_service=None,transport=SyntheticModels().transport)
    assert service.discovery.semantic_runner is not None
    client=service.discovery.semantic_runner.embedding
    await service.close()
    assert client._client.is_closed
