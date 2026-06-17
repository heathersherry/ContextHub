from __future__ import annotations

from pydantic import BaseModel


class HandoffPacket(BaseModel):
    sender: str
    recipient: str
    task_intent: str
    required_object_ids: list[str] = []
    source_artifacts: list[str] = []
    expected_action: str = ""
    context_versions: list[str] = []
    missing_fields: list[str] = []

    def static_missing(self, required: set[str]) -> list[str]:
        present = {
            key
            for key, value in self.model_dump().items()
            if value not in (None, [], "")
        }
        return sorted(required - present)
