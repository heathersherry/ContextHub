# MemDerive integration into the current research worktree

Merged locally on 2026-10-28 (Hong Kong, UTC+8) into `codex/memory-research`.
The branch advanced from `7df675667` to `de0b351ab`, bringing in the MemDerive
adapter, session/event provenance, and the first real Q1 baseline record.

The target also contained 37 modified tracked files and four new source/test
files. These edits were included in isolated validation and restored as
**uncommitted changes**, together with the merge resolutions. No user edits
were committed implicitly, and no remote push was performed. The resulting
working code therefore cannot be identified by HEAD alone.

Validated implementation SHA-256:
`908aacb4655926a008bb19dff056202e4084e49ddd201945143bd83d94da2f14`.

## Resolution and validation

- Preserve the target's single ordered `items` extraction format, bounded
  forgetting candidates, `inform` handling, ended-state updates and incremental
  binding. Add required source message numbers without sorting the items array.
- Preserve duplicate operations as no-ops, including unchanged original sources.
  MemDerive follows this shared policy; it does not add a benchmark-specific write.
- Keep precise source coordinates for direct writes and recursive versioned
  premises for derived memories. Update the adapter fixtures to the current
  extraction contract and duplicate policy.
- **466 tests passed, zero failures/errors/skips, in 56.24 seconds.** Coverage
  includes the shared memory core, MEME adapter/runner, public API/SDK, and the
  pinned official MemDerive Q1/Q2/Q3 execution fixtures. The test database was
  `contexthub_memderive_merge_test_20261028`; all model and embedding responses
  were deterministic fixtures. No paid model experiment was run for this merge.
- The primary worktree's transferred files and source hash match the tested
  integration worktree. Original user untracked history directories were retained.

## Running both benchmarks

Use [MEME-32k](../../experiments/meme32k/README.md) and
[MemDerive](../../integrations/memderive/README.md) from this same checkout. They
share `src/contexthub/memory`; maintain separate databases, namespaces, run output
directories and metrics. Freeze source/prompts for the duration of each run.
Before a formal comparison, commit the combined working changes; the MEME batch
wrapper intentionally requires clean tracked source. MemDerive preparation hashes
actual code/prompts and must be repeated after changes.

Message-level provenance adds a required database column and extraction field.
**Create fresh experiment databases with the current schema.** Existing databases
and saved old-format extraction outputs were not migrated or rewritten. Old
experiment results remain historical measurements of their recorded code.
The prior tripops_w1 score of 41/140 is not a score for this merged version.

## Evidence and recovery

- [Validation summary](validation-memderive-merge-20261028/summary.json)
- [Test log](validation-memderive-merge-20261028/regression-final.log)
- [JUnit results](validation-memderive-merge-20261028/regression-final.xml)
- [Historical real Q1 run](MEMDERIVE-TRIPOPS-W1-Q1-20261027.md)

The original changes are backed up in Git stash commit
`8dd10e3853587d3c1c8e485c140d177c4312040b`. They are already present in the working
tree; do not apply that stash again onto the merged working tree. The original
and combined patches, selected new source/test files, and hash manifests are
retained under `outputs/memderive-merge-20261028/` (ignored by Git).

The complete prior real-run artifacts were also copied into
`outputs/memderive-tripops-w1-q1-20261027-r1/` in this checkout. Their recorded
configuration paths still describe the original run. The adapter worktree and
its original raw artifacts remain available for historical comparison.
