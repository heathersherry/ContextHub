"""Independent exports/evaluation: never imported by session maintenance.

Only this side receives privileged scoring labels. It cannot write a checkpoint.
Official answer grading, internal path/state review and human semantic review
remain separate; no lexical proxy is promoted to an internal correctness score.
"""
from contexthub.models.knowledge import ContractError, canonical_hash
from .chronology import QuestionVault
from .ledger import canonical_records, export_cost


def session_costs(records, *, wall_seconds=None, complete_export=True, paired_rq1=False):
    rows = canonical_records(records)
    groups = {
        'P1': [r for r in rows if r.identity.stage == 'P1' and r.identity.model_role != 'embedding'],
        'P1-reference': [r for r in rows if r.identity.stage == 'P1-reference'],
        'P2': [r for r in rows if r.identity.stage == 'P2'],
        'embedding': [r for r in rows if r.identity.model_role == 'embedding'],
        'answer': [r for r in rows if r.identity.stage == 'answer'],
        'judge': [r for r in rows if r.identity.stage == 'judge'],
    }
    covered = [r for group in groups.values() for r in group]
    if len(covered) != len(rows):
        raise ContractError('session_cost_unclassified_attempt')
    shared = [r for r in rows if r.identity.stage == 'P1' and
              not r.identity.operation.endswith((':dependency', ':identity'))]
    logical = None
    if paired_rq1:
        logical = {arm: export_cost(shared + [r for r in rows
            if r.identity.stage in ('P1','P1-reference')
            and r.identity.operation.endswith((':dependency', ':identity'))
            and r.identity.strategy.endswith(':'+arm)], wall_seconds=None,
            attribution='logical-P1-'+arm+'-including-shared', complete_export=complete_export)
            for arm in ('selective','full-history')}
    return dict(local_work_measurement='not_measured; rule operations retained in checkpoint audits',
        rq1_logical=logical, parts={name: export_cost(group,wall_seconds=None,attribution=name,
                    complete_export=complete_export) for name,group in groups.items()},
        episode=export_cost([r for r in rows if r.identity.stage not in ('judge','P1-reference')],wall_seconds=wall_seconds,
                    attribution='complete-episode-with-embedding-without-judge',complete_export=complete_export),
        research_total=export_cost(rows,wall_seconds=wall_seconds,attribution='all-research-calls',complete_export=complete_export),
        sharing='paired-RQ1-same-extraction; no-cross-trajectory-semantic-cache')


async def judge_session_result(result, *, boundary, question_directory, labels,
                               judge, run_id, replicate_id, profile):
    """Use an independently authorized OfficialJudgeRunner supplied by the caller.

    Missing phases/answers keep their Cas/Abs label denominator and count as
    failure. A failed-before-exam phase never needs its future question opened.
    """
    if result.get('protocol') != 'session-evidence-v2' or result.get('episode_id') != boundary.episode_id:
        raise ContractError('session_judge_identity_mismatch')
    labels = [l for l in labels if l.episode_id == boundary.episode_id]
    vault = QuestionVault(question_directory,boundary)
    judgments, attempts = [], []
    for phase in ('before','after'):
        seal = result['states'].get(phase)
        if seal is None:
            questions, answers = {}, {}
        else:
            bundle = vault.release(phase,seal)
            questions = {q.question_id:q.text for q in bundle.questions}
            answers = {a['question_id']:a for a in result['answers'].get(phase,[])}
            if len(answers) != len(result['answers'].get(phase,[])) or not answers.keys() <= questions.keys():
                raise ContractError('session_judge_question_slots_invalid')
        for label in labels:
            qid = getattr(label,phase+'_question_id')
            row = answers.get(qid)
            if row is None:
                # Explicit failure record, not absent sample or a fabricated QA.
                judgments.append(dict(record_id=label.record_id,phase=phase,question_id=qid,
                    label_sha256=label.record_sha256,run_id=str(run_id),replicate_id=replicate_id,
                    profile=profile,correct=False,reason='arm_or_answer_missing',error=result.get('failure')))
                continue
            if row['state_sha256'] != seal['sha256']:
                raise ContractError('session_judge_answer_state_mismatch')
            judgment, records = await judge.judge(sidecar=label,phase=phase,question=questions[qid],
                answer_row=row,run_id=run_id,replicate_id=replicate_id,profile=profile)
            judgments.append(judgment.model_dump(mode='json'))
            attempts.extend(r.model_dump(mode='json') for r in records)
    return dict(judgments=judgments,attempts=attempts,result_sha256=canonical_hash(result),
                main_analysis=['Cas','Abs'],maintenance_feedback=False)


async def judge_session_comparison(comparison, *, judges, boundary, question_directory,
                                   labels, run_id, replicate_id):
    """Reject divergent judge identities before scoring any trajectory."""
    profiles = {'local/base-only', 'full/strong-only', 'adaptive'}
    if set(judges) != profiles or set(comparison['profiles']) != profiles:
        raise ContractError('session_judge_profiles_required')
    identities = {canonical_hash(dict(config=canonical_hash(j.config), source=canonical_hash(j.source))) for j in judges.values()}
    if len(identities) != 1:
        raise ContractError('session_judge_configuration_mismatch')
    results = {}
    for profile, result in comparison['profiles'].items():
        results[profile] = await judge_session_result(result, boundary=boundary,
            question_directory=question_directory, labels=labels, judge=judges[profile],
            run_id=run_id, replicate_id=replicate_id, profile=profile)
    return dict(judge_configuration_sha256=identities.pop(), profiles=results)
