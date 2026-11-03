"""Program routing for explicit verbatim requests and normal maintenance afterward."""
import pytest

from contexthub.memory.jev_intent_classifier import JevIntentClassifier
from .helpers import add, extraction_response, item, row, session
from .test_jev_direct import Context, choice_client, jev, plan_response  # noqa: F401

LOG = (
    'CertificateVerificationError: certificate chain validation failed for crysthene-db-primary.internal.\n'
    'Subject CN does not match expected hostname; chain depth 2 unverified.\n'
    'Observed at 2024-07-22T11:09:47Z by tls_handshake_listener.\n'
    'Rotate the cert via Cryptara Vault and restart the connection pool to recover.'
)
REQUEST = 'Remember this error log word for word: "' + LOG + '"'


@pytest.mark.asyncio
@pytest.mark.parametrize('text', [
    REQUEST,
    'Please remember the following verbatim:\nA literal quotation.',
    'remember these lines word for word: "Do not remember this. If A, then B. Forget X."',
])
async def test_explicit_verbatim_request_never_reaches_jev(text):
    requests, context = [], Context()
    client = choice_client([], requests)  # Any network request fails the fixture.
    try:
        assert await JevIntentClassifier(client, retry=None).classify([{'text': text}], context) == ['inform']
    finally:
        await client.close()
    assert not requests
    assert context.steps == [('p2_intent.program', {
        'rule': 'explicit_verbatim_memory_request', 'intents': {'C1': 'inform'},
    })]


@pytest.mark.asyncio
@pytest.mark.parametrize('text', [
    'Do you remember this log word for word: an error?',
    'Do not remember this log word for word: an error.',
    'If I ask, remember this log word for word: an error.',
    'The document says "Remember this log word for word: an error."',
    'I remember this log word for word: an error.',
    'Remember to correct the old log word for word: an error.',
    'Remember this log word for word:',
    'Remember this log: the words "word for word" are part of its contents.',
    'Remember that my address is Beijing.',
])
async def test_other_forms_still_use_jev(text):
    requests, context = [], Context()
    client = choice_client(['hold'], requests)
    try:
        assert await JevIntentClassifier(client, retry=None).classify([{'text': text}], context) == ['hold']
    finally:
        await client.close()
    assert len(requests) == 1
    assert not any(stage == 'p2_intent.program' for stage, _ in context.steps)


@pytest.mark.asyncio
async def test_mixed_batch_preserves_positions_and_jev_decisions():
    requests, context = [], Context()
    client = choice_client(['inform', 'correction'], requests)
    contents = [{'text': t} for t in ['I live in Beijing.', REQUEST,
                'My earlier claim was wrong.', 'Please remember this verbatim: A quote.']]
    try:
        choices = await JevIntentClassifier(client, retry=None).classify(contents, context)
    finally:
        await client.close()
    assert choices == ['inform', 'inform', 'correction', 'inform']
    assert list(requests[0]['questions']) == ['C1', 'C3']
    assert context.steps[0][1]['intents'] == {'C2': 'inform', 'C4': 'inform'}
    assert set(context.steps[-1][1]['answers']) == {'C1', 'C3'}


@pytest.mark.asyncio
async def test_sw004_log_is_saved_after_program_routing(env, jev):
    # Replay the actual failed extraction text; no classifier call is permitted.
    env.responses.append(extraction_response(rules=[], contents=[item(REQUEST)]))
    refs = await env.full_pipeline.ingest(env.trace, 1, [{'role': 'user', 'content': REQUEST}])
    assert len(refs) == 1
    saved = await env.store.read_ref(env.trace, f'{refs[0][0]}:{refs[0][1]}')
    assert saved['accepted'] and saved['status'] == 'fresh'
    assert LOG in saved['text']
    assert not jev[1] and not env.responses


@pytest.mark.asyncio
async def test_program_routing_keeps_normal_old_memory_maintenance(env, jev):
    c = await session(env, 1)
    old = await add(env, c, 'The old log is ConnectionError.')
    env.responses.extend([
        extraction_response(rules=[], contents=[item(REQUEST)]),
        plan_response(('new', None, REQUEST)),
    ])
    refs = await env.full_pipeline.ingest(env.trace, 2, [{'role': 'user', 'content': REQUEST}])
    assert len(refs) == 1 and refs[0][0] != (await row(env, old))['memory_id']
    assert len(env.prompts) == 2  # Extraction and normal inform maintenance.
    assert 'Maintain memory for incoming information classified as inform' in env.prompts[-1]['messages'][0]['content']
    assert (await row(env, old))['status'] == 'fresh'
    assert not jev[1] and not env.responses
