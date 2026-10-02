"""Experiment deadline; cancellation preserves attempts, never retries an episode."""

import asyncio
from contextlib import asynccontextmanager


@asynccontextmanager
async def episode_deadline(seconds):
    # Core/client cancellation finalizers run before TimeoutError is reported.
    # This is a cooperative deadline, not a process kill or crash recovery claim.
    async with asyncio.timeout(seconds):
        yield


def episode_failure(episode, replicate, reason):
    return dict(
        episode_id=episode,
        replicate_id=replicate,
        status="error",
        reason=reason,
        cost_export_complete=False,
        disposition="preserve_state_no_automatic_retry",
    )
