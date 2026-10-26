"""One eligibility and context assembly path for applications and evaluations."""

from dataclasses import dataclass
import json

from .contracts import material, split_ref
from .public import MemoryContextItem, MemoryContextResult
from .retriever import answer_purpose


class ContextBudgetError(ValueError):
    pass


def answer_context(materials, purpose):
    if purpose in ("answer", "policy"):
        result = {
            "purpose": purpose,
            "memories": [
                {"text": m["text"], "type": "fact"}
                for m in materials
                if m["status"] == "fresh" and m["type"] == "fact"
            ],
        }
        if purpose == "policy":
            result["conditional_rules"] = [
                {"text": m["text"]}
                for m in materials
                if m["status"] == "fresh" and m["type"] == "rule"
            ]
        return result
    return {
        "purpose": purpose,
        "time_note": "Dates in statement text are fact-time evidence. created_in_session and timestamp only describe ingestion order, not effective dates.",
        "memories": [
            {k: v for k, v in m.items() if k not in ("ref", "basis_refs")}
            for m in materials
        ],
    }


@dataclass
class ContextSelection:
    mode: str
    materials: list[dict]
    rendered: str
    budget_exhausted: bool = False


class ContextBuilder:
    def __init__(self, store, retriever):
        self.store, self.retriever = store, retriever

    async def select(self, question, context, *, mode="auto", max_chars=None):
        purpose = answer_purpose(question) if mode == "auto" else mode
        if purpose not in ("answer", "policy", "history"):
            raise ValueError("invalid context mode")
        if max_chars is not None and max_chars < 1:
            raise ContextBudgetError("positive character budget required")

        def render(items):
            return json.dumps(
                answer_context(items, purpose), default=str, ensure_ascii=False
            )

        materials, exhausted = [], False
        rendered = render(materials)
        if max_chars is not None and len(rendered) > max_chars:
            raise ContextBudgetError(
                "context budget is smaller than the empty context envelope"
            )
        if context.session_seq:
            hits = await self.retriever.search(
                question,
                context,
                through_session=context.session_seq,
                purpose=purpose,
                memory_type="fact" if purpose == "answer" else None,
            )
            for hit in hits:
                candidate = material(hit.memory)
                trial = render([*materials, candidate])
                if max_chars is not None and len(trial) > max_chars:
                    exhausted = True
                    break  # Preserve ranking and whole assertions, never cut a rule in half.
                materials.append(candidate)
                rendered = trial
        return ContextSelection(purpose, materials, rendered, exhausted)

    async def source_sessions(self, scope, ref, cutoff):
        # These are session-level sources, not fabricated event-level witnesses.
        pending, seen, sources = [ref], set(), set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            row = await self.store.read_ref(scope, current)
            if row is None or row["status"] == "deleted" or row["invalidated"]:
                continue
            if row["created_in_session"] > cutoff:
                continue
            if row["origin"] == "derived":
                pending.extend(row["basis_refs"])
            else:
                record = await self.store.sources(scope, *split_ref(current))
                if record:
                    sources.update(record["session_seq"])
        return sorted(s for s in sources if s <= cutoff)

    async def result(self, selection, context):
        return MemoryContextResult(
            namespace=context.trace.namespace,
            sequence=context.session_seq,
            mode=selection.mode,
            context=selection.rendered,
            budget_exhausted=selection.budget_exhausted,
            items=[
                MemoryContextItem(
                    **{
                        k: m[k]
                        for k in (
                            "ref",
                            "text",
                            "type",
                            "origin",
                            "status",
                            "temporal_scope",
                            "basis_refs",
                        )
                    },
                    source_sessions=await self.source_sessions(
                        context.trace, m["ref"], context.session_seq
                    ),
                )
                for m in selection.materials
            ],
        )
