"""Small deterministic lexical retrieval; hits are candidates, never proofs."""
import re

_STOP = set('my is are was were the a an and or if will be to of for in on it this that changes change ordinary chat'.split())


def terms(text):
    tokens = set(re.findall(r"[a-z0-9_]+", text.casefold())) - _STOP
    # Preserve Chinese adjacency without pretending to perform entity resolution.
    for run in re.findall(r'[\u3400-\u9fff]+', text):
        tokens.update(run[i:i+2] for i in range(max(1, len(run)-1)))
    return tokens


def overlap(left, right):
    return len(terms(left) & terms(right))


def source_search(sources, query):
    return sorted((s for s in sources if any(overlap(query, m.content) for m in s.messages)),
                  key=lambda s: (-sum(overlap(query, m.content) for m in s.messages), s.ordinal))
