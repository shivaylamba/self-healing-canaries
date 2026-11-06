---
name: resource-exhaustion
summary: Errors caused by running out of something (memory, connections, file handles, goroutines, rate limits, disk).
error_types: [oom, timeout, too_many_connections, rate_limited]
template_regex: (out of memory|oom|too many|exhausted|timeout|deadline exceeded|connection refused|pool|429|503)
path_kinds: [code, deploy]
revert_preference: fix_first
---
# Skill: resource exhaustion

Typical shape: errors rise with time or load rather than with one kind of request; restarts
temporarily help.

1. Prometheus: request rate, latency and error rate over time for the canary hash. Do errors
   grow over the canary's life (leak) or track load (capacity)?
2. Look for pod restarts and OOMKilled in the logs around the window.
3. In the offending change, look for new allocations per request, unbounded caches, missing
   `Close()`, new goroutines without cancellation, smaller pools, lower limits.
4. Fix the leak or the limit; do not only raise resources unless the change legitimately
   needs more.
5. Guard: a canary metric on the resource (memory, open connections) in the AnalysisTemplate
   as a dry-run metric first.
