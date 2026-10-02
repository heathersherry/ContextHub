"""Opt-in composition for the session protocol; no settings, keys or DB loading.

Callers supply an already configured repository. Merely importing/building this
module never connects it. Real execution requires a separate, explicit grant;
legacy run authorizations do not qualify.
"""
from dataclasses import dataclass
from uuid import UUID, uuid4

import httpx
from contexthub.models.knowledge import ContractError, canonical_hash
from contexthub.models.execution import CallIdentity
from contexthub.llm.chat_client import OpenAIChatClient
from contexthub.llm.recorded_embedding import RecordedEmbeddingClient
from contexthub.llm.retry_policy import RecordedCall
from contexthub.services.execution_ledger import ExecutionLedger
from contexthub.services.session_memory import PgSessionStore, Checkpoint, answer_view
from contexthub.services.session_models import RecordedSessionModels, SessionConfiguration
from contexthub.services.session_knowledge_service import SessionKnowledgeService
from .answer_prompt import OfficialAnswerConfig, render_answer
from .session_runner import SessionEpisodeRunner


@dataclass(frozen=True)
class SessionExecutionGrant:
    protocol: str
    configuration_sha256: str
    answer_configuration_sha256: str
    account_id: str
    answer_account_id: str
    stream_id: UUID
    # A caller must supply the reviewed NEW run authorization artifact hash.
    authorization_sha256: str


class RecordedSessionAnswerer:
    def __init__(self, config, client, ledger, account_id, stream_id):
        self.config, self.client, self.ledger = config, client, ledger
        self.account_id, self.stream_id = account_id, stream_id
        self.call_ids = []

    async def __call__(self, question, sealed):
        checkpoint = Checkpoint.model_validate(sealed['state'])
        if checkpoint.stream_id != self.stream_id:
            raise ContractError('session_answer_scope_mismatch')
        view = answer_view(checkpoint)
        identity = CallIdentity(account_id=self.account_id, execution_id=self.stream_id,
            call_id=uuid4(), strategy='session-answer', stage='answer', operation=sealed['phase'],
            model_role='answer', code_version='session-evidence-v2', config_sha256=canonical_hash(self.config))
        self.call_ids.append(identity.call_id)
        return await self.client.complete(render_answer(self.config, question, view),
            max_tokens=self.config.max_tokens,
            call=RecordedCall(identity, self.config.retry, self.ledger, self.config.price))


@dataclass
class SessionComponents:
    service: SessionKnowledgeService
    answerer: RecordedSessionAnswerer
    runner: SessionEpisodeRunner
    clients: tuple

    async def close(self):
        for client in self.clients:
            await client.close()


def create_session_components(*, repository, config: SessionConfiguration,
        answer_config: OfficialAnswerConfig, account_id: str, answer_account_id: str,
        stream_id: UUID, keys=None, transport=None, grant=None, ledger=None):
    # Validate before constructing transports. No credentials are loaded here.
    config = SessionConfiguration.model_validate(config.model_dump(mode='json'))
    answer_config = OfficialAnswerConfig.model_validate(answer_config.model_dump(mode='json'))
    if account_id == answer_account_id:
        raise ContractError('session_answer_requires_separate_account')
    if not isinstance(transport, httpx.MockTransport):
        expected = SessionExecutionGrant('session-evidence-v2', config.fingerprint(),
            canonical_hash(answer_config), account_id, answer_account_id, stream_id,
            getattr(grant, 'authorization_sha256', ''))
        if (grant != expected or len(expected.authorization_sha256) != 64
                or any(c not in '0123456789abcdef' for c in expected.authorization_sha256)):
            raise ContractError('new_session_execution_authorization_required')
        if not keys or not set((*config.models, 'embedding', 'answer')) <= keys.keys():
            raise ContractError('session_explicit_keys_required')
    keys = keys or {role: 'offline-placeholder' for role in (*config.models, 'embedding', 'answer')}
    sink = ledger if ledger is not None else ExecutionLedger(repository)
    def chat(role, cfg):
        return OpenAIChatClient(keys[role], base_url=cfg.base_url, model=cfg.model,
            timeout=cfg.retry.timeout_seconds, temperature=cfg.temperature,
            request_parameters=cfg.request_parameters, transport=transport)
    clients = {role: chat(role, cfg) for role, cfg in config.models.items()}
    embedding = RecordedEmbeddingClient(config.embedding, api_key=keys['embedding'], transport=transport)
    answer_client = chat('answer', answer_config)
    models = RecordedSessionModels(config, clients, embedding, sink, account_id, stream_id)
    service = SessionKnowledgeService(config, PgSessionStore(repository, account_id, stream_id), models)
    answerer = RecordedSessionAnswerer(answer_config, answer_client, sink, answer_account_id, stream_id)
    return SessionComponents(service, answerer, SessionEpisodeRunner(service, answerer),
                             (*clients.values(), embedding, answer_client))


async def run_session_arm(components, runtime, boundary, *, question_directory):
    """One complete trajectory. A failed arm stays failed; never choose another graph."""
    try:
        try:
            result = await components.runner.run(runtime, boundary, question_directory=question_directory)
            result['arm_status'] = 'complete'
        except Exception as exc:
            # Keep the planned arm and any completed before answers. Do not open
            # future question packages just to manufacture failed rows here.
            from copy import deepcopy
            result = deepcopy(getattr(components.runner, 'progress', dict(
                protocol='session-evidence-v2', sessions=[], states={}, answers={}, judge_executed=False)))
            result.update(arm_status='failed', failure=type(exc).__name__,
                          failure_details=deepcopy(components.service.failure))
        result['configuration_sha256'] = components.service.config.fingerprint()
        result['answer_call_ids'] = [str(i) for i in components.answerer.call_ids]
        result['last_session_call_ids'] = [str(i) for i in components.service.models.call_ids]
        result['episode_id'] = runtime.episode_id
        result['input_sha256'] = runtime.input_sha256
        return result
    finally:
        await components.close()


async def run_session_comparison(components_by_profile, runtime, boundary, *, question_directory):
    """RQ2: fixed three empty-start trajectories, identical controls except policy."""
    profiles = ('local/base-only','full/strong-only','adaptive')
    if set(components_by_profile) != set(profiles):
        raise ContractError('session_comparison_profiles_required')
    scopes, controls, answer_controls = set(), set(), set()
    for profile in profiles:
        component = components_by_profile[profile]
        cfg = component.service.config
        if cfg.profile != profile or cfg.search != 'selective' or cfg.paired_rq1:
            raise ContractError('session_comparison_control_mismatch')
        controls.add(canonical_hash(cfg.model_dump(mode='json',exclude={'profile'})))
        if not hasattr(component.answerer, 'config'):
            raise ContractError('session_answer_configuration_required')
        answer_controls.add(canonical_hash(component.answerer.config))
        scopes.update((component.service.store.account_id,component.answerer.account_id))
    if len(controls) != 1 or len(answer_controls) != 1 or len(scopes) != 6:
        raise ContractError('session_comparison_requires_same_controls_separate_accounts')
    results = {}
    for profile in profiles:
        results[profile] = await run_session_arm(components_by_profile[profile],runtime,boundary,
                                                 question_directory=question_directory)
    return dict(protocol='session-evidence-v2',rq2_pairing='same-empty-start-continuous-trajectories',
                input_sha256=runtime.input_sha256,profiles=results,judge_executed=False)
