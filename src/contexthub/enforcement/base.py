from __future__ import annotations

from abc import ABC, abstractmethod

from contexthub.db.repository import ScopedRepo
from contexthub.enforcement.context import Boundary, EnforcementContext
from contexthub.enforcement.decision import GuardrailDecision


class Guardrail(ABC):
    name: str = "guardrail"
    applies_to: frozenset[Boundary] = frozenset()

    @abstractmethod
    async def check(self, db: ScopedRepo, ec: EnforcementContext) -> GuardrailDecision:
        ...
