"""Summarize a completed paired selection probe without any model calls."""

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import statistics

from scripts.memory_research.selection_probe import read_jsonl, sha, write_json


def means(rows):
    names = [score['scorer'] for score in rows[0]['scores']]
    return {name.removeprefix('derivemem_'): statistics.mean(
        next(s['value'] for s in row['scores'] if s['scorer'] == name) for row in rows)
        for name in names}


def summarize(root):
    manifest = json.loads((root / 'manifest.json').read_text())
    status = json.loads((root / 'status.json').read_text())
    if status['status'] != 'complete':
        raise ValueError('probe is incomplete; do not publish a completed comparison')
    results = read_jsonl(root / 'results.jsonl')
    ids = manifest['question_ids']
    if Counter((r['question_id'], r['arm']) for r in results) != Counter(
            (qid, arm) for qid in ids for arm in ('baseline', 'selected')):
        raise ValueError('paired results missing or duplicated')
    unchanged = {path: sha(Path(path)) == digest for path, digest in manifest['files'].items()}
    if not all(unchanged.values()):
        raise ValueError('frozen source or input changed')
    raw_path, = [Path(p) for p in manifest['files'] if p.endswith('/raw_results.jsonl')]
    archived = [r for r in read_jsonl(raw_path) if r['question_id'] in ids]
    arms = {arm: [r for r in results if r['arm'] == arm] for arm in ('baseline', 'selected')}
    metrics = {arm: means(rows) for arm, rows in {'archived': archived, **arms}.items()}
    by_id = {qid: {r['arm']: r for r in results if r['question_id'] == qid} for qid in ids}
    changes = {}
    for metric in ('answer_accuracy', 'witnessed_accuracy'):
        def value(row):
            return next(s['value'] for s in row['scores'] if s['scorer'] == 'derivemem_' + metric)
        changes[metric] = {label: [qid for qid, pair in by_id.items()
            if compare(value(pair['selected']), value(pair['baseline']))]
            for label, compare in (('improved', lambda a, b: a > b),
                                   ('regressed', lambda a, b: a < b),
                                   ('unchanged', lambda a, b: a == b))}
    finished = [r for r in read_jsonl(root / 'attempts.jsonl') if r['phase'] == 'finished']
    selection = read_jsonl(root / 'selections.jsonl')
    usage = {}
    for stage in ('selection', 'baseline', 'selected'):
        rows = [r for r in finished if r['identity']['stage'] == stage]
        durations = [r['seconds'] for r in (selection if stage == 'selection' else arms[stage])]
        usage[stage] = {'attempts': len(rows), 'failed_attempts': sum(r['outcome'] != 'success' for r in rows),
            'missing_or_inconsistent_usage': sum(r['usage']['completeness'] != 'complete' for r in rows),
            'input_tokens': sum(r['usage']['input_tokens'] or 0 for r in rows),
            'output_tokens': sum(r['usage']['output_tokens'] or 0 for r in rows),
            'reported_reasoning_tokens': sum(r['usage']['reasoning_tokens'] or 0 for r in rows),
            'calls_reporting_reasoning': sum((r['usage']['reasoning_tokens'] or 0) > 0 for r in rows),
            'total_seconds': sum(r['wall_seconds'] for r in rows),
            'mean_seconds': statistics.mean(durations),
            'median_seconds': statistics.median(durations),
            'actual_models': dict(Counter(r['actual_model'] for r in rows))}
    families = {category: {arm: means([r for r in rows if r['category'] == category])
                          for arm, rows in arms.items()}
                for category in manifest['category_counts']}
    prepared = {r['question_id']: r for r in read_jsonl(root / 'prepared.jsonl')}
    checks = {
        'all_frozen_files_unchanged': all(unchanged.values()),
        'exactly_15_original_items_per_answer': all(len(r['retrieved']) == 15 for r in results),
        'selected_refs_unique_and_from_candidate_union': all(
            len(r['selected_refs']) == len(set(r['selected_refs'])) == 15
            and set(r['selected_refs']) <= set(r['candidate_refs']) for r in selection),
        'baseline_evidence_and_prompts_reconstructed_exactly': manifest['baseline_evidence_and_prompts_exact_match'],
        'selected_text_unchanged': all(
            all(e['text'] == next(m['text'] for m in prepared[r['question_id']]['union']
                                 if m['ref'] == e['metadata']['ref']) for e in r['retrieved'])
            for r in arms['selected']),
        'no_ingestion_or_embedding_calls': set(r['identity']['stage'] for r in finished)
                                         == {'selection', 'baseline', 'selected'},
    }
    if not all(checks.values()):
        raise ValueError('experiment invariant failed')
    summary = {'status': status, 'manifest': manifest, 'metrics': metrics, 'paired_changes': changes,
               'families': families, 'usage': usage, 'checks': checks,
               'q001': {arm: {'answer': json.loads(row['answer']['answer']),
                              'scores': {s['scorer']: s['value'] for s in row['scores']}}
                        for arm, row in by_id['q001'].items()}}
    write_json(root / 'summary.json', summary)
    return summary


def report(summary, root):
    n = len(summary['manifest']['question_ids'])
    local = timezone(timedelta(hours=8))
    finished = datetime.fromisoformat(summary['status']['finished_at']).astimezone(local).isoformat()
    lines = ['# MemDerive candidate selection: paired development probe', '',
        f'Status: completed. {n} fixed development questions from `tripops_w1`; not a full-world or full-benchmark result.', '',
        f'Finished: {finished} (UTC+8). Total elapsed: {summary["status"]["seconds"] / 60:.2f} minutes.', '',
        '## Controlled comparison', '',
        '- Reused the 2026-10-27 final memory snapshot, embeddings, and logged retrieval candidates. No database connection or writes.',
        '- Selected 20 questions at evenly spaced official positions, including q001, before inference. No outcome-based selection.',
        '- Control: original top 15, freshly answered. Treatment: union of saved keyword/vector candidates plus the original supplemental entity/topic hits, one DeepSeek selection, freshly answered.',
        '- Both arms return exactly 15 unmodified, source-attributed memory items. No graph expansion, query rewriting, or ingestion changes.',
        '- Selector: DeepSeek official `deepseek-flash`, reasoning `low`, no explicit temperature. Returns existing IDs only. One selection output attempt; shared transport retries retained.',
        '- Answer: OpenLux `gpt-5.6-sol`, reasoning `none`, temperature 0, max completion tokens 2048. Official v9 prompt and parser unchanged. Shared recorded transport sends the corresponding official wire parameters.',
        '- Scoring: all eight official deterministic provenance-v2 metrics. Gold is used only after answer generation, never by the selector or answer prompt.',
        '- Answer-call order alternates between arms by question. No prompt tuning during this run.', '',
        '## Scores', '',
        '| Metric | Archived same sample | Fresh control | Selected candidates |',
        '| --- | ---: | ---: | ---: |']
    for metric in summary['metrics']['baseline']:
        lines.append('| ' + metric + ' | ' + ' | '.join(
            f'{summary["metrics"][arm][metric] * 100:.2f}%' for arm in ('archived', 'baseline', 'selected')) + ' |')
    lines += ['', f'Every aggregate has {n} question observations. Citation precision/recall/F1 are query macro means. Witnessed accuracy requires a correct answer and precision and recall each >= 0.5; it does not require all witnesses.', '',
              '## Paired changes', '']
    for metric, changes in summary['paired_changes'].items():
        lines.append(f'- {metric}: improved {len(changes["improved"])} ({", ".join(changes["improved"]) or "none"}); regressed {len(changes["regressed"])} ({", ".join(changes["regressed"]) or "none"}); unchanged {len(changes["unchanged"])}.')
    lines += ['', '## Question families', '',
        '| Family | Questions | Control answer | Selected answer | Control witnessed | Selected witnessed |',
        '| --- | ---: | ---: | ---: | ---: | ---: |']
    for family, arms in summary['families'].items():
        lines.append(f'| {family} | {summary["manifest"]["category_counts"][family]} | ' + ' | '.join(
            f'{arms[arm][metric] * 100:.2f}%' for metric in ('answer_accuracy', 'witnessed_accuracy')
            for arm in ('baseline', 'selected')) + ' |')
    lines += ['', '## Usage and latency', '',
        '| Stage | Requests | Input tokens | Output tokens | Mean seconds | Median seconds |',
        '| --- | ---: | ---: | ---: | ---: | ---: |']
    for stage, usage in summary['usage'].items():
        lines.append(f'| {stage} | {usage["attempts"]} | {usage["input_tokens"]:,} | {usage["output_tokens"]:,} | {usage["mean_seconds"]:.2f} | {usage["median_seconds"]:.2f} |')
    lines += ['', 'No dollar estimate is claimed without verified provider prices. Selection adds a sequential model call. Provider-reported reasoning tokens are included within output tokens.', '',
        '## Interpretation limits', '',
        '- This tests the combined candidate-retention-plus-selection change, not the independent contribution of either component.',
        '- The snapshot predates the expanded rule gate and factual-retention prompt. Missing rules and omitted source facts cannot be restored by selection.',
        '- One response per question and arm, one development world, and only 20 questions: a directional probe, not a general quality claim or evidence for changing the default.',
        '- Entity linking and multi-hop supplementation remain unimplemented. Selection can only choose existing candidates.',
        '- Reasoning is requested off for answers; provider-reported reasoning usage is retained in summary.json and is not assumed to be zero.', '',
        '## Validation and artifacts', '',
        '- Seven focused tests passed for union preservation, gold exclusion, original-text selection, budget, duplicate IDs and unknown IDs.',
        '- An additional existing official-judge prompt-loading test passed with the correct local MEME source path; its database-dependent companion was skipped because no validation database was configured.',
        '- All 20 baseline evidence lists and answer prompts matched their archived counterparts exactly before API calls. Final checks verify frozen files, 15-item budgets, candidate membership and unmodified memory text.',
        '- Re-scoring the archived responses reproduced all 160 official metric values exactly (20 questions x 8 metrics).',
        f'- Raw artifacts: `{root.resolve()}` (`manifest.json`, `prepared.jsonl`, `model-config.json`, `attempts.jsonl`, `responses/`, `selections.jsonl`, `results.jsonl`, `summary.json`).',
        '- This MemDerive small-sample probe is not entered in the MEME-32k 100-case index.', '']
    lines += ['## Additional diagnostics', '',
        'Mean and median times above are per-question stage durations including transport retries and backoff. Token totals include only reported usage; failed attempts with missing usage are not assumed free.', '']
    for stage, usage in summary['usage'].items():
        lines.append(f'- {stage}: {usage["failed_attempts"]} failed attempts; {usage["missing_or_inconsistent_usage"]} attempts with missing/inconsistent usage; {usage["calls_reporting_reasoning"]} calls reported reasoning tokens ({usage["reported_reasoning_tokens"]:,} tokens).')
    lines += ['', 'q001 answers and citations:', '']
    for arm, row in summary['q001'].items():
        lines.append(f'- {arm}: answer={json.dumps(row["answer"].get("answer"), ensure_ascii=False)}; witness IDs={row["answer"].get("witness_ids")}; witness precision={row["scores"]["derivemem_witness_precision"]:.3f}; recall={row["scores"]["derivemem_witness_recall"]:.3f}.')
    lines += ['', '## Reproduction', '',
        'Use the pinned DerivEval source on `PYTHONPATH`, the existing baseline directory, and a new output directory. Provider credentials are read from the local provider configuration and are never copied into the result files.', '',
        '```bash',
        'PYTHONPATH=src:sdk/src:.:/private/tmp/contexthub-deriveval-reference/src .venv/bin/python -m scripts.memory_research.selection_probe \\',
        '  --baseline outputs/memderive-tripops-w1-q1-20261027-r1 \\',
        '  --output outputs/memderive-selection-probe-new-run --count 20 --execute',
        '```', '',
        'Omit `--execute` for an offline preparation/reconstruction check. Each invocation requires a new output directory. The probe is experimental and is not wired into the shared engine or either benchmark adapter.', '']
    return '\n'.join(lines)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    summary = summarize(args.root)
    args.report.write_text(report(summary, args.root))
    print(json.dumps({'metrics': summary['metrics'], 'changes': summary['paired_changes'],
                      'usage': summary['usage'], 'checks': summary['checks']}, indent=2))
