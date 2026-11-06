# Original fusion selection: API, SDK and DeepAgents verification

Date: 2026-11-06T11:51:52.775728+08:00 (Asia/Hong_Kong).

The search request now accepts `original_fusion="rrf"` or `"weighted"`.
The default remains `weighted`. API request validation, Python SDK forwarding,
DeepAgents tool arguments and the original-passage coordinator use the same
choice. The server retrieval trace records it. This is a parameter/wiring check,
not another relevance benchmark or a weight-selection experiment.

## Contract

- `rrf`: the original equal-weight formula `1/(60+vector_rank) +
  1/(60+BM25_rank)`, with an absent discovery route contributing zero. No cosine
  completion adds vector-rank votes for candidates outside the vector list.
- `weighted`: the current min/max normalized score fusion with fixed vector
  weight 0.8 and BM25 weight 0.2. Existing stored vectors complete missing
  candidate cosine scores; no embeddings are regenerated.
- Only original-document passage fusion changes by this parameter. Candidate
  discovery, full-corpus BM25, source/version/range identity and the outer merge
  with other context types keep their existing behavior. Invalid values are
  rejected by the API/tool request schemas. No additional weight/config knobs
  or alternate API routes are introduced.

```python
await client.search(query, top_k=5, original_fusion="rrf")
await client.search(query, top_k=5, original_fusion="weighted")
```

The same argument is accepted in `POST /api/v1/search` JSON and
`contexthub_search` tool arguments.

## Checks

190 focused tests pass; one generic live-database fixture test is skipped.
Tests exercise the default, both explicit selections, invalid input, SDK/tool
HTTP forwarding, original RRF ranks and absence of score completion in RRF,
weighted scoring and existing retrieval/source boundaries.

The actual local PostgreSQL → FastAPI → SDK → DeepAgents tool path runs both
choices against the existing one-document Microarchitecture sample and the two
saved queries, with `top_k=5` and vector budget 15. RRF results exactly equal the
preserved BM25/RRF replay; weighted results exactly equal the preserved weighted
replay, including bodies, scores, provenance, versions and ranges. Requests and
server traces carry the requested choice. No model/provider calls occur.

| Selection | Query | Returned section IDs | Matches preserved result |
|---|---|---|---|
| rrf | literal | 10, 17, 13, 8, 14 | Yes |
| rrf | semantic | 16, 4, 8, 13, 12 | Yes |
| weighted | literal | 17, 13, 10, 8, 14 | Yes |
| weighted | semantic | 8, 10, 16, 13, 4 | Yes |

The four search calls and twenty section reads roll back all database bookkeeping.
Protected sample/old-index state and historical artifacts have identical
fingerprints. Existing uncommitted changes are preserved. No source is reingested,
no index is rebuilt, and no answering model is run. Provider calls: **0**.

## Evidence

- [Replay harness](../../integrations/hydrabench/validate_fusion_selection.py)
- [RRF results and scores](artifacts/20261106-fusion-selection/rrf/comparison.json)
- [Weighted results and scores](artifacts/20261106-fusion-selection/weighted/comparison.json)
- [Verification summary](artifacts/20261106-fusion-selection/validation.json)
- [Protected state before](artifacts/20261106-fusion-selection/rrf/protected-before.json)
- [Protected state after](artifacts/20261106-fusion-selection/weighted/protected-after.json)

Each selection's artifact directory also contains the complete original Agent
JSON, actual API requests/responses and local HTTP paths for both queries.
The [weighted-fusion report](2026-11-06-WEIGHTED-FUSION.md) retains the full
before/after original bodies and the previously observed ranking tradeoff.
