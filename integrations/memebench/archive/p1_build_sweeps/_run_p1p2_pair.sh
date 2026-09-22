#!/bin/bash
# Serial driver: 做法甲+做法乙 end-to-end, two strong-tier configs x two hops.
# Order: exp1 hop1 -> exp1 hop2 -> exp2 hop1 -> exp2 hop2.
# Each run has its own --out dir, so checkpoints never cross-skip (the resume
# `done` map keys on episode_id alone, and hop1/hop2 share episode ids).
# Serial by design: parallel runs contend on the same slow proxy and inflate
# each other's latency ~5x (measured 2026-08-10).
set -u
cd /Users/sherrylin/Documents/PythonProjects/ContextHub

COMMON=(
  --provider openlux
  --chat-model gpt-4.1-mini
  --extract-model gpt-4.1-mini
  --raw-dialogue --edge-mode discovered
  --cascade
  --cascade-cheap-model gpt-4o-mini
  --tau-disamb 0.5 --tau-cand 0.4 --tau-edge 0.4 --cascade-k 5
  --p2-cascade
  --p2-cheap-model gpt-4o-mini
  --case-timeout 900
)

run () {  # $1=strong model  $2=hop  $3=outdir
  echo "=== START $(date +%H:%M:%S)  strong=$1 hop=$2 -> $3"
  CONTEXTHUB_INTEGRATION=1 .venv/bin/python3 -m integrations.memebench.run_eval \
    --hop "$2" "${COMMON[@]}" \
    --cascade-strong-model "$1" \
    --oracle-model "$1" \
    --out "integrations/memebench/runs/$3"
  echo "=== END $(date +%H:%M:%S)  exit=$?  strong=$1 hop=$2"
}

run gpt-4.1-mini 1 p1p2_hop1_strong41mini
run gpt-4.1-mini 2 p1p2_hop2_strong41mini
run gpt-5.5      1 p1p2_hop1_strong55
run gpt-5.5      2 p1p2_hop2_strong55
echo "=== ALL DONE $(date +%H:%M:%S)"
