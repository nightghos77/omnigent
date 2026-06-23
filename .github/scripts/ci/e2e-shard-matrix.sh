#!/usr/bin/env bash
# Emits the e2e shard matrix as `matrix=<json>` on $GITHUB_OUTPUT, or an EMPTY
# matrix ({"include":[]}) to skip. Empty yields zero jobs and thus NO check-runs
# -- the point of the indirection: a job-level `if:` skip would instead leave a
# check-run with an unexpanded `E2E Tests (shard ${{ matrix.shard_id }}/...)` name.
#
# Skips draft PRs, and fork PRs only when REQUIRES_SECRETS=true -- forks can't
# read secrets, so a secret-bearing leg (the e2e-ui native job) runs them via the
# fork-e2e/** mirror instead. Mock-LLM legs leave it unset and run forks like CI.
#
# Env in:  EVENT_NAME, IS_DRAFT, IS_FORK, REQUIRES_SECRETS (default false), NUM_SHARDS.
# Shared by e2e.yml and e2e-ui.yml (differ in NUM_SHARDS / REQUIRES_SECRETS).

set -euo pipefail

skip=false
if [[ "${IS_DRAFT:-false}" == "true" ]]; then
  skip=true
fi
if [[ "$EVENT_NAME" == "pull_request" && "${IS_FORK:-false}" == "true" \
      && "${REQUIRES_SECRETS:-false}" == "true" ]]; then
  skip=true
fi

if [[ "$skip" == "true" ]]; then
  echo 'matrix={"include":[]}' >> "$GITHUB_OUTPUT"
  echo "skip: empty matrix (event=$EVENT_NAME draft=${IS_DRAFT:-} fork=${IS_FORK:-})"
  exit 0
fi

inc=""
for ((i = 0; i < NUM_SHARDS; i++)); do
  inc+="{\"shard_id\":$i,\"num_shards\":$NUM_SHARDS},"
done
echo "matrix={\"include\":[${inc%,}]}" >> "$GITHUB_OUTPUT"
echo "run: $NUM_SHARDS shards (event=$EVENT_NAME)"
