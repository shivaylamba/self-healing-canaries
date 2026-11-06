---
name: config-drift
summary: The code did not change but configuration did (env vars, flags, ConfigMaps, overlays, limits).
error_types: [config_invalid, missing_config]
template_regex: (config|env|flag|not set|missing .*key|invalid value)
path_globs: [deploy/*, "*.env", "*configmap*", "*values*.yaml"]
path_kinds: [config, deploy]
revert_preference: first
---
# Skill: configuration drift

Typical shape: a deploy that changes only configuration, or two environments whose
configuration differs in a way the code depends on.

1. `get_recent_changes`: list commits that touched only `deploy/` or config files before the incident.
2. Diff the environments' overlays (`deploy/overlays/staging` vs `deploy/overlays/prod`).
3. Find the value the failing code path reads, and what it is in each environment.
4. A config-only change is cheap and safe to revert: if the revert was already opened by the
   incident system, explain it in your report and propose the corrected config as the fix.
5. Guard: validate the value at startup (fail fast) or add it to the overlay render check in CI.
