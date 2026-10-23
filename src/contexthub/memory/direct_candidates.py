"""Cheap evidence gate for new-intent fact candidates, not a semantic decision."""

from collections import Counter
from math import log


DIRECT_CANDIDATE_POLICY = {
    "strong_similarity": 0.50,
    "supported_similarity": 0.35,
    "keyword_max_document_fraction": 0.50,
}

# PostgreSQL already removes English stop words. These additional lexemes are
# generic subjects or temporal/intensity modifiers, not domain classifications.
GENERIC_LEXEMES = frozenset({
    "user", "person", "team", "group", "someone", "someon", "something", "someth",
    "current", "currently", "recent", "recently", "late", "lately",
    "now", "still", "already", "realli", "really", "pretti", "pretty",
})


def select_candidates(scores, corpus, query_lexemes):
    """Return per-version decisions using per-matter document frequency.

    Multiple historical versions count as one document. Unknown query words
    do not dilute an informative shared word when the new value changes.
    """
    documents = {}
    for row in corpus:
        documents.setdefault(str(row["memory_id"]), set()).update(row["lexemes"])
    frequency = Counter(word for words in documents.values() for word in words)
    size = len(documents)
    max_frequency = max(1, int(size * DIRECT_CANDIDATE_POLICY["keyword_max_document_fraction"]))
    min_weight = 1 + log((size + 1) / (max_frequency + 1))
    query_words = set(query_lexemes) - GENERIC_LEXEMES
    decisions = []
    for row in scores:
        similarity = row["similarity"]
        if similarity is None:
            raise ValueError("Direct candidate must be indexed before filtering")
        shared = sorted(query_words.intersection(row["lexemes"]))
        weights = {word: 1 + log((size + 1) / (frequency[word] + 1)) for word in shared}
        informative = [word for word in shared if weights[word] >= min_weight]
        if row["identifiers"]:
            reason = "exact_identifier"
        elif similarity >= DIRECT_CANDIDATE_POLICY["strong_similarity"]:
            reason = "strong_vector"
        elif similarity >= DIRECT_CANDIDATE_POLICY["supported_similarity"] and row["entities"]:
            reason = "vector_entity"
        elif similarity >= DIRECT_CANDIDATE_POLICY["supported_similarity"] and informative:
            reason = "vector_keyword"
        else:
            reason = "insufficient_support"
        decisions.append({
            "ref": row["ref"], "similarity": similarity,
            "shared_keyword_weights": weights, "informative_keywords": informative,
            "matched_entities": row["entities"], "matched_identifiers": row["identifiers"],
            "selected": reason != "insufficient_support", "reason": reason,
        })
    return decisions, {"matters": size, "max_keyword_document_frequency": max_frequency,
                       "min_keyword_weight": min_weight}
