---
name: latency-regression
summary: The canary is slower than stable (p95/p99) without necessarily failing requests.
error_types: [timeout, deadline_exceeded]
template_regex: (slow|timeout|deadline|latency|took)
path_kinds: [code]
revert_preference: fix_first
---
# Skill: latency regression

1. Compare canary vs stable p95 from `http_server_duration_milliseconds_bucket`, scoped by
   `rollouts_pod_template_hash`, over the canary's life.
2. If only some requests are slow, slice by the business attributes in the logs.
3. In the offending change look for new synchronous calls, N+1 lookups, missing caching,
   retries with backoff on the hot path, lock contention.
4. Fix the hot path. Guard: promote `canary-vs-stable-p95` from dry run to a gate once it is
   stable, or add a benchmark test.
