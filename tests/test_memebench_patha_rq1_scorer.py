from uuid import uuid4
import pytest
from contexthub.models.knowledge import (
    ContextVersion,
    GraphSnapshot,
    DependencyEdge,
    PendingDependency,
    SourceSnapshot,
    canonical_hash,
    text_hash,
)
from contexthub.discovery.contracts import NodeMapping
from integrations.memebench.patha.rq1_scorer import (
    GraphEvidence,
    PathReview,
    StepReview,
    score_rq1,
)
from integrations.memebench.patha.scoring_contract import build_scoring_contract
from integrations.memebench.patha.schemas import canonical_sha256
from s4_helpers import evidence, NodeDB


async def fixture():
    db = NodeDB()
    e = await evidence(db, "A determines B.")
    ns = [ContextVersion(context_id=uuid4(), version=1) for _ in range(3)]
    edges = tuple(
        DependencyEdge(
            upstream=ns[i],
            downstream=ns[i + 1],
            semantic_basis="fixture rule",
            evidence=(e,),
            provenance=("original",),
        )
        for i in range(2)
    )
    g = GraphSnapshot(
        account_id="s2-a",
        id=uuid4(),
        graph_version=1,
        nodes=tuple(ns),
        edges=edges,
        config_sha256="a" * 64,
    )
    path = {
        "path_id": "path_0",
        "entities": ["a", "b", "c"],
        "direction": "event_root_to_target",
        "p2_traversable_if_all_edges_delivered": True,
        "edges": [
            {
                "source_entity": x,
                "target_entity": y,
                "dialogue_evidence": [
                    {
                        "source_id": "source",
                        "turn_index": 0,
                        "excerpt": e.excerpt,
                        "excerpt_sha256": text_hash(e.excerpt),
                    }
                ],
            }
            for x, y in [("a", "b"), ("b", "c")]
        ],
    }
    ref = {
        "record_id": "rec",
        "episode_id": "episode",
        "necessary_path_candidates": [path],
        "data_quality_notes": ["fixture retained note"],
    }
    ref["record_sha256"] = canonical_sha256(ref)
    contract = build_scoring_contract(
        [
            dict(
                record_id="rec", answer_pair_status="scoreable", path_status="scoreable"
            )
        ],
        [ref],
        artifact_hashes={},
        contract_validator_code={},
    )
    delivered = GraphEvidence(
        graph=g,
        reference=g.reference(),
        status="complete",
        mappings=tuple(
            NodeMapping(
                candidate_id=uuid4(),
                proposition_id=uuid4(),
                node=n,
                text="fixture",
                evidence=(e,),
            )
            for n in ns
        ),
        source_map=(("source", e.source),),
        source_texts={e.source.sha256: e.excerpt},
        source_snapshots=(
            SourceSnapshot.model_validate(db.artifacts[e.source.id]["payload"]),
        ),
    )
    review = PathReview(
        record_id="rec",
        path_id="path_0",
        reference_record_sha256=ref["record_sha256"],
        graph=g.reference(),
        reviewer_id="synthetic-fixture-author",
        review_method="offline_human_semantic_review",
        review_version="fixture-1",
        nodes=tuple(ns),
        steps=tuple(
            StepReview(
                upstream=edge.upstream,
                downstream=edge.downstream,
                edge_sha256=canonical_hash(edge),
                reference_step_sha256=canonical_sha256(step),
                evidence=(e,),
                verdict="PASS",
                rationale="fixture exact semantic step",
            )
            for edge, step in zip(edges, path["edges"])
        ),
    )
    return contract, ref, delivered, review


@pytest.mark.asyncio
async def test_complete_path_and_any_valid_alternative():
    c, p, g, r = await fixture()
    assert score_rq1(c, [p], {"episode": g}, (r,))["covered"] == 1
    bad = r.model_copy(update={"path_id": "nonmatching"})
    assert score_rq1(c, [p], {"episode": g}, (bad, r))["covered"] == 1
    assert score_rq1(c, [p], {}, ())["denominator"] == 1
    assert score_rq1(c, [p], {"episode": g}, ())["rows"][0]["reasons"] == [
        "semantic_mapping_not_reviewed"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "disconnected",
        "reversed",
        "no_evidence",
        "version",
        "pending",
        "empty",
        "timeout",
        "error",
        "unknown",
        "wrong_source",
        "bad_step",
    ],
)
async def test_failures_retain_denominator(failure):
    c, p, d, r = await fixture()
    g = d.graph
    updates = {}
    if failure == "disconnected":
        updates["edges"] = g.edges[:1]
    elif failure == "reversed":
        updates["edges"] = (
            g.edges[0].model_copy(
                update={
                    "upstream": g.edges[0].downstream,
                    "downstream": g.edges[0].upstream,
                }
            ),
        )
    elif failure == "no_evidence":
        d = d.model_copy(update={"source_texts": {}})
    elif failure == "version":
        r = r.model_copy(
            update={
                "nodes": (r.nodes[0].model_copy(update={"version": 2}), *r.nodes[1:])
            }
        )
    elif failure == "pending":
        updates["pending"] = (
            PendingDependency(
                upstream=g.edges[0].upstream,
                downstream=g.edges[0].downstream,
                reason="unknown",
            ),
        )
    elif failure == "empty":
        updates = {"nodes": (), "edges": ()}
    elif failure in ("timeout", "error"):
        d = d.model_copy(update={"status": failure})
    elif failure == "unknown":
        r = r.model_copy(
            update={
                "steps": (
                    r.steps[0].model_copy(update={"verdict": "UNKNOWN"}),
                    r.steps[1],
                )
            }
        )
    elif failure == "wrong_source":
        d = d.model_copy(
            update={
                "source_map": (
                    (
                        "source",
                        d.source_map[0][1].model_copy(update={"sha256": "b" * 64}),
                    ),
                )
            }
        )
    elif failure == "bad_step":
        r = r.model_copy(
            update={
                "steps": (
                    r.steps[0].model_copy(update={"reference_step_sha256": "c" * 64}),
                    r.steps[1],
                )
            }
        )
    if updates:
        g = GraphSnapshot.model_validate(dict(g.model_dump(), **updates))
        d = d.model_copy(update={"graph": g, "reference": g.reference()})
        r = r.model_copy(update={"graph": g.reference()})
    score = score_rq1(c, [p], {"episode": d}, (r,))
    assert (
        score["covered"] == 0
        and score["denominator"] == 1
        and score["rows"][0]["reasons"]
    )
    assert score["rows"][0]["data_quality_notes"] == ["fixture retained note"]


@pytest.mark.asyncio
async def test_all_frozen_records_required_and_no_endpoint_only_success():
    c, p, g, r = await fixture()
    with pytest.raises(ValueError):
        score_rq1(c, [], {"episode": g}, (r,))
    shortened = r.model_copy(
        update={"nodes": (r.nodes[0], r.nodes[-1]), "steps": (r.steps[0],)}
    )
    assert score_rq1(c, [p], {"episode": g}, (shortened,))["covered"] == 0


@pytest.mark.asyncio
async def test_valid_alternative_when_original_necessary_path_is_broken():
    c, p, d, r = await fixture()
    original = d.graph
    edge = DependencyEdge(
        upstream=r.nodes[0],
        downstream=r.nodes[-1],
        semantic_basis="fixture alternative rule",
        evidence=original.edges[0].evidence,
        provenance=("original",),
    )
    graph = original.model_copy(update={"edges": (edge,)})
    ref_step = dict(
        p["necessary_path_candidates"][0]["edges"][0],
        source_entity="a",
        target_entity="c",
    )
    alternative = {
        "path_id": "alternative",
        "entities": ["a", "c"],
        "direction": "event_root_to_target",
        "p2_traversable_if_all_edges_delivered": True,
        "edges": [ref_step],
    }
    p = dict(
        p, necessary_path_candidates=[*p["necessary_path_candidates"], alternative]
    )
    p["record_sha256"] = canonical_sha256(
        {k: v for k, v in p.items() if k != "record_sha256"}
    )
    d = d.model_copy(update={"graph": graph, "reference": graph.reference()})
    r = r.model_copy(
        update={
            "reference_record_sha256": p["record_sha256"],
            "graph": graph.reference(),
        }
    )
    alt = r.model_copy(
        update={
            "path_id": "alternative",
            "nodes": (r.nodes[0], r.nodes[-1]),
            "steps": (
                StepReview(
                    upstream=edge.upstream,
                    downstream=edge.downstream,
                    edge_sha256=canonical_hash(edge),
                    reference_step_sha256=canonical_sha256(ref_step),
                    evidence=edge.evidence,
                    verdict="PASS",
                    rationale="fixture independently valid alternative",
                ),
            ),
        }
    )
    result = score_rq1(c, [p], {"episode": d}, (r, alt))
    assert (
        result["covered"] == 1 and result["rows"][0]["matched_path_id"] == "alternative"
    )


async def dialogue_fixture(original, turn_index, spans, *, role="user"):
    """Exact synthetic source coordinates; no production matching helper used."""
    import json

    c, p, d, r = await fixture()
    db = NodeDB()
    whole = await evidence(db, original)
    refs = tuple(
        whole.model_copy(
            update={
                "start": start,
                "end": end,
                "excerpt": original[start:end],
                "excerpt_sha256": text_hash(original[start:end]),
            }
        )
        for start, end in spans
    )
    edges = tuple(e.model_copy(update={"evidence": refs}) for e in d.graph.edges)
    graph = d.graph.model_copy(update={"edges": edges})
    d = d.model_copy(
        update={
            "graph": graph,
            "reference": graph.reference(),
            "source_map": (("source", whole.source),),
            "source_texts": {whole.source.sha256: original},
            "source_snapshots": (
                SourceSnapshot.model_validate(db.artifacts[whole.source.id]["payload"]),
            ),
            "mappings": tuple(
                m.model_copy(update={"evidence": refs}) for m in d.mappings
            ),
        }
    )
    content = json.loads(original)[turn_index]["content"]
    for step in p["necessary_path_candidates"][0]["edges"]:
        step["dialogue_evidence"] = [
            dict(
                source_id="source",
                turn_index=turn_index,
                role=role,
                excerpt=content,
                excerpt_sha256=text_hash(content),
            )
        ]
    p["record_sha256"] = canonical_sha256(
        {k: v for k, v in p.items() if k != "record_sha256"}
    )
    r = r.model_copy(
        update={
            "graph": graph.reference(),
            "reference_record_sha256": p["record_sha256"],
            "steps": tuple(
                s.model_copy(
                    update={
                        "evidence": refs,
                        "edge_sha256": canonical_hash(e),
                        "reference_step_sha256": canonical_sha256(step),
                    }
                )
                for s, e, step in zip(
                    r.steps, edges, p["necessary_path_candidates"][0]["edges"]
                )
            ),
        }
    )
    return c, p, d, r


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["unicode", "ascii", "custom_escapes"])
@pytest.mark.parametrize(
    "granularity", ["whole_source", "whole_turn", "narrow", "two_fragments"]
)
async def test_dialogue_citation_granularity_and_json_escapes(encoding, granularity):
    import json

    content = (
        '前提 A supports "B".\nB supports C. 路径 /end and slash \\. Unrelated note.'
    )
    original = json.dumps(
        [
            {"role": "assistant", "content": "Other turn"},
            {"metadata": {"content": content}, "content": content, "role": "user"},
        ],
        ensure_ascii=encoding == "ascii",
        indent=2,
    )
    if encoding == "custom_escapes":
        original = original.replace("/end", r"\/end").replace(
            "supports", r"\u0073upports"
        )
    encoded = original[original.rindex('"content": "') + len('"content": "') :]
    start = len(original) - len(encoded)
    end = original.index('",\n', start)
    if granularity == "whole_source":
        spans = [(0, len(original))]
    elif granularity == "whole_turn":
        spans = [(start, end)]
    elif granularity == "narrow":
        spans = [(start, original.index("Unrelated", start))]
    else:
        middle = original.index(
            "B supports C" if encoding != "custom_escapes" else r"B \u0073upports C",
            start,
        )
        spans = [
            (start, middle - 2),
            (
                middle,
                middle
                + len(
                    "B supports C."
                    if encoding != "custom_escapes"
                    else r"B \u0073upports C."
                ),
            ),
        ]
    c, p, d, r = await dialogue_fixture(original, 1, spans)
    result = score_rq1(c, [p], {"episode": d}, (r,))
    assert result["covered"] == result["denominator"] == 1, result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wrong", ["other_turn", "metadata", "role", "version", "empty_span"]
)
async def test_same_text_at_wrong_location_is_not_witness_evidence(wrong):
    import json

    content = "A supports B. B supports C."
    original = json.dumps(
        [
            {"role": "user", "content": content},
            {"role": "user", "content": content, "metadata": {"content": content}},
        ]
    )
    positions = []
    i = 0
    while (i := original.find(content, i)) >= 0:
        positions.append(i)
        i += len(content)
    start = positions[0 if wrong == "other_turn" else 2 if wrong == "metadata" else 1]
    end = start if wrong == "empty_span" else start + len(content)
    c, p, d, r = await dialogue_fixture(
        original, 1, [(start, end)], role="assistant" if wrong == "role" else "user"
    )
    if wrong == "version":
        d = d.model_copy(
            update={
                "source_map": (
                    (
                        "source",
                        d.source_map[0][1].model_copy(update={"sha256": "b" * 64}),
                    ),
                )
            }
        )
    result = score_rq1(c, [p], {"episode": d}, (r,))
    assert result["denominator"] == 1 and result["covered"] == 0, result


@pytest.mark.asyncio
async def test_partial_json_escape_is_not_original_semantic_evidence():
    original = r'[{"role":"user","content":"\u524d A supports B. B supports C."}]'
    start = original.index(r"\u524d")
    c, p, d, r = await dialogue_fixture(original, 0, [(start + 1, start + 5)])
    score = score_rq1(c, [p], {"episode": d}, (r,))
    assert score["denominator"] == 1 and score["covered"] == 0
    # The same source and reviewed path with complete escapes remains usable.
    c, p, d, r = await dialogue_fixture(
        original, 0, [(start, original.index('"}', start))]
    )
    assert score_rq1(c, [p], {"episode": d}, (r,))["covered"] == 1
