from __future__ import annotations

import logging
from dataclasses import replace

from contexthub.db.repository import ScopedRepo

from .result import LongDocRetrievalResult

logger = logging.getLogger(__name__)


class LongDocRetrievalCoordinator:
    def __init__(self):
        self._strategies: dict[str, object] = {}

    def register_strategy(self, name: str, strategy) -> None:
        self._strategies[name] = strategy

    async def retrieve(
        self,
        db: ScopedRepo,
        query: str,
        candidates: list[dict],
        *,
        strategy: str = "tree",
    ) -> list[dict]:
        long_docs = [candidate for candidate in candidates if candidate.get("file_path")]
        if not long_docs or strategy not in self._strategies:
            return candidates

        strat = self._strategies[strategy]
        all_results: list[LongDocRetrievalResult] = []

        if strategy == "tree":
            for doc in long_docs:
                try:
                    base_score = float(doc.get("_rerank_score", doc.get("cosine_similarity", 0.0)))
                    results = await strat.retrieve(
                        db,
                        query,
                        doc["id"],
                        doc["uri"],
                        doc["file_path"],
                        base_score=base_score,
                    )
                except Exception:
                    logger.warning("Tree retrieval failed for %s", doc.get("uri"), exc_info=True)
                    results = []
                best = results[:1]
                if best:
                    all_results.extend(best)
        elif strategy == "keyword":
            try:
                all_results = await strat.retrieve(db, query, long_docs)
            except Exception:
                logger.warning("Keyword retrieval failed", exc_info=True)
                all_results = []

        return self._merge_results(candidates, all_results)

    async def search_originals(self, db, query, documents, *, query_embedding=None, top_k=30):
        """Fuse exact original ranges; each distinct range remains a candidate."""
        if not documents:
            return []
        routes = []
        keyword = self._strategies.get("keyword")
        if keyword is not None:
            routes.append(await keyword.retrieve(db, query, documents))
        vector = self._strategies.get("vector")
        if vector is not None and query_embedding is not None:
            routes.append(await vector.retrieve(db, query_embedding, documents, top_k))
        fused, scores = {}, {}
        versions = {doc["id"]: doc["version"] for doc in documents}
        for route in routes:
            for rank, result in enumerate(sorted(route, key=lambda r: r.relevance_score, reverse=True), 1):
                key = (result.context_id, versions[result.context_id], result.snippet_offset)
                scores[key] = scores.get(key, 0.0) + 1.0 / (60 + rank)
                strategy = "hybrid" if key in fused else result.strategy
                fused[key] = replace(result, strategy=strategy)
        results = [replace(result, relevance_score=scores[key]) for key, result in fused.items()]
        by_id = {doc["id"]: doc for doc in documents}
        return [self._materialize(by_id[result.context_id], result)
                for result in sorted(results, key=lambda r: r.relevance_score, reverse=True)]

    @staticmethod
    def _materialize(candidate: dict, result: LongDocRetrievalResult) -> dict:
        return {**candidate, "snippet": result.snippet, "section_id": result.section_id,
                "snippet_offset": result.snippet_offset, "retrieval_strategy": result.strategy,
                "_rerank_score": result.relevance_score}

    def _merge_results(
        self,
        candidates: list[dict],
        results: list[LongDocRetrievalResult],
    ) -> list[dict]:
        best_by_context: dict[object, LongDocRetrievalResult] = {}
        for result in results:
            current = best_by_context.get(result.context_id)
            if current is None or result.relevance_score > current.relevance_score:
                best_by_context[result.context_id] = result

        merged: list[dict] = []
        for candidate in candidates:
            result = best_by_context.get(candidate.get("id"))
            if result is None:
                merged.append(candidate)
                continue
            merged.append(self._materialize(candidate, result))
        return merged
