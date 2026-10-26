"""Use the existing ContextHub server through its Python SDK.

Set CONTEXTHUB_URL, CONTEXTHUB_API_KEY, CONTEXTHUB_ACCOUNT_ID,
CONTEXTHUB_AGENT_ID and MEMORY_NAMESPACE to a new conversation namespace.
"""

import asyncio
import os

from contexthub_sdk import ContextHubClient, MemoryIngestRequest, MemoryContextRequest


async def main():
    async with ContextHubClient(
        os.environ["CONTEXTHUB_URL"],
        os.environ["CONTEXTHUB_API_KEY"],
        os.environ["CONTEXTHUB_ACCOUNT_ID"],
        os.environ["CONTEXTHUB_AGENT_ID"],
        timeout=1800,
    ) as client:
        namespace = os.environ["MEMORY_NAMESPACE"]
        await client.memory.ingest(
            MemoryIngestRequest(
                namespace=namespace,
                sequence=1,
                messages=[
                    {"role": "user", "content": "The Atlas project owner is Wang."}
                ],
            )
        )
        await client.memory.ingest(
            MemoryIngestRequest(
                namespace=namespace,
                sequence=2,
                messages=[
                    {
                        "role": "user",
                        "content": "The Atlas project owner has changed to Li.",
                    }
                ],
            )
        )
        materials = await client.memory.context(
            MemoryContextRequest(
                namespace=namespace,
                query="Who owns Atlas?",
                max_chars=4000,
            )
        )
        # Pass this to the application's answer model alongside the user's query.
        print(materials.context)
        print([item.model_dump() for item in materials.items])


if __name__ == "__main__":
    asyncio.run(main())
