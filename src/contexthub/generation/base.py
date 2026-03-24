"""Content generator: produces L0/L1 summaries from raw content."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GeneratedContent:
    l0: str
    l1: str
    llm_tokens_used: int = 0


_TRUNCATE_L0 = 80
_TRUNCATE_L1 = 300


class ContentGenerator:
    def generate(
        self,
        context_type: str,
        raw_content: str,
        metadata: dict | None = None,
    ) -> GeneratedContent:
        if context_type == "skill":
            return self._generate_skill(raw_content)
        if context_type == "memory":
            return self._generate_memory(raw_content)
        return self._generate_fallback(raw_content)

    def _generate_skill(self, raw: str) -> GeneratedContent:
        first_line = raw.split("\n", 1)[0].strip()
        l0 = first_line[:_TRUNCATE_L0] if first_line else raw[:_TRUNCATE_L0]
        l1 = raw[:_TRUNCATE_L1]
        return GeneratedContent(l0=l0, l1=l1)

    def _generate_memory(self, raw: str) -> GeneratedContent:
        l0 = raw[:_TRUNCATE_L0]
        l1 = raw[:_TRUNCATE_L1]
        return GeneratedContent(l0=l0, l1=l1)

    def _generate_fallback(self, raw: str) -> GeneratedContent:
        l0 = raw[:_TRUNCATE_L0]
        l1 = raw[:_TRUNCATE_L1]
        return GeneratedContent(l0=l0, l1=l1)
