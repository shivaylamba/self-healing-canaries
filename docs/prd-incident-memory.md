# PRD: Incident Memory for the AI SRE

| | |
|---|---|
| Status | Implemented on `feat/incident-memory`, **off by default**, not deployed |
| Owner | Shivay Lamba |
| Builds on | Self-healing canaries (Argo Rollouts canary gate + AI SRE on AURA) |

## 1. Problem

The AI SRE today is reactive and has no memory.

- **One trigger.** It wakes only when Argo Rollouts aborts a canary.
- **One playbook.** Every investigation starts from scratch with the same procedure, whatever the failure is.
- **Nothing is kept.** The release ledger lives in an `emptyDir`, so a pod restart erases it.
- **No change history.** Commits are not tracked over time, so "what changed before this?" is answered from scratch each time.
- **Code fixes only.** Its only action is a code-fix PR. There is no fast, safe mitigation for a failure it has seen before.

Real systems fail in many more ways than the two bugs in the demo. To deal with them, the AI SRE needs to remember what failed, what it looked like and what fixed it, and use that the next time.

## 2. Goals

1. **Track every change.** Record each commit on `main`: its files, the kind of change, its PR, and whether a revert would be safe.
2. **Mine logs into patterns.** Turn error logs into templates, counted per ReplicaSet and broken down by business attribute.
3. **Open incidents with a signature.** Each incident records what it looked like, in terms that survive across incidents.
4. **Match against past incidents.** Pass the past root cause and fix to the agent as a hypothesis, and flag recurrences.
5. **Route to a specialised skill.** Pick a playbook per failure class, not one prompt for everything.
6. **Revert on a known pattern.** When the evidence is strong, open a revert PR deterministically, as mitigation, while the agent works on the root-cause fix.
7. **Track the lifecycle and outcomes:** open, then mitigating, then resolved or guarded. Recurrences are flagged.

## 3. Non-goals (v1)

- **No auto-merge, anywhere.** Humans merge every PR, including reverts.
- No paging, on-call rotation or ticketing integration (PagerDuty, incident.io, Jira).
- The agent doesn't read traces yet. Traces are collected (Jaeger) but signatures use logs and metrics.
- No cross-service dependency graph. v1 is per-service: `payment`.
- No ML-based anomaly detection. Patterns are templates plus counts, and the gate stays the canary analysis.

## 4. Users

- **On-call engineer:** sees an incident with its evidence, a match to a past incident, and a PR waiting, instead of a bare alert.
- **PR reviewer:** reviews a revert or fix PR whose body explains the attribution and the evidence.
- **Engineering lead:** watches MTTR, recurrence and the guarded rate to see whether the system is learning.

## 5. Concepts

| Concept | Definition |
|---|---|
| **Change** | A commit on `main` (or a PR head commit), with its files, kinds (`code`, `deploy`, `config`, `infra`, `test`, `docs`, `ci`), PR, and whether a revert would be safe. |
| **Log pattern** | A template mined from error logs, for example `psp amount <*> exceeds limit <*> for JPY`. Counted per (env, ReplicaSet hash, minute) with business slices (currency, region, SKU). |
| **Signature** | What an incident looks like: its pattern templates, error types, dominant slice (`checkout_currency=JPY`), and the change's kinds and directories. |
| **Attribution** | **`hash`**: the failing pattern exists only on the canary's ReplicaSet (`rollouts_pod_template_hash`), so the commit is proven responsible. **`temporal`**: the failure is only correlated with recent changes by time. |
| **Skill** | A markdown playbook for one failure class, with match rules (`ai-sre/skills/*.md`). |
| **Revert** | A PR that puts every file the offending commit touched back to its parent version. Built by deterministic code, never by the model. |

**Why hash attribution matters.** Most incident tools link a log spike to a deploy by timing ("it started at 3:05, something deployed at 3:04"). Here the platform stamps the ReplicaSet hash on every metric and log line. A pattern that appears only on the canary's hash is evidence, not a guess. That is the only basis on which v1 will open a revert.

## 6. Lifecycle

```
                   fix or revert PR opened              that PR's image passes the canary
 rollout aborted ─▶ open ───────────────────▶ mitigating ─────────────────────────────▶ resolved
 new pattern  ───▶ (temporal)                                │ in the incident's own env
                                                             └── fix added a guard ───▶ guarded
 A new incident whose signature matches a resolved or guarded one at similarity ≥ 0.8
 gets recurrence_of set: the guard didn't hold.
```

- A fix that passes in **another** environment (staging, for a prod incident) is recorded as `fix_passed_other_env` but doesn't resolve the incident.
- **Guards:**
  - a test (`*_test.go`);
  - a traffic profile (`loadgen.env`);
  - an analysis-template change;
  - or the agent reporting `guard_added`.

## 7. Functional requirements

| ID | Requirement | Where |
|---|---|---|
| FR1 | Poll GitHub for commits on `main` every 60s. Classify paths, detect the PR, and decide whether a revert is safe. Fetch PR head commits on demand. | `ai-sre/changes.py` |
| FR2 | Read error logs per environment every 60s through Grafana's Loki datasource proxy. Mine templates (Drain-style, no dependencies) and store hits per (pattern, env, hash, minute) with slices. Bucket by log timestamp so rescans don't double-count. Pattern ids survive restarts. | `ai-sre/patterns.py` |
| FR3 | When a rollout is aborted: rescan the last 15 min; compute the canary-only patterns, the offending change and the signature; match against closed incidents; select a skill; open the incident. | `ai-sre/incident_service.py` |
| FR4 | Similarity is weighted Jaccard: templates 0.5, error types 0.2, slice 0.2, paths 0.1. A component with no data on either side is left out, and the weights are renormalised. | `ai-sre/incidents.py` |
| FR5 | Skills are markdown with front-matter match rules. The best-scoring skill is given to the agent, with `generic` as the fallback. The agent can also call `list_skills` / `get_skill`. | `ai-sre/skills.py`, `ai-sre/skills/` |
| FR6 | Pattern-based revert. See the safety rules below. | `ai-sre/revert.py` |
| FR7 | Drive the lifecycle from Rollouts notifications and agent reports. Every transition is appended to `incident_events`, which is the audit trail. | `incident_service.py` |
| FR8 | Six new ledger MCP tools for the agent (`get_incident`, `find_similar_incidents`, `get_log_patterns`, `get_recent_changes`, `list_skills`, `get_skill`). REST `GET /api/incidents[/{id}]`. A read-only `/incidents` page in the demo UI. | `ai-sre/app.py`, `demo-ui/incidents.html` |
| FR9 | The ledger is persisted on a PVC, so the memory survives restarts. | `ai-sre/k8s.yaml` |

## 8. Safety rules

1. **Revert only when everything holds:** `AUTO_REVERT_PR=true`; attribution is `hash`; env is `prod` (staging failures are fixed on the PR branch, so there is nothing on `main` to revert); the change is safely revertable; **and** either
   - a past incident at similarity ≥ 0.8 was resolved by a revert, or
   - the selected skill has `revert_preference: first` (config drift).
2. **A revert is refused when:**
   - any file it would touch changed on `main` after the offending commit (that would silently undo someone else's work);
   - the commit is a merge commit, is itself a revert, or touches migrations, schema, SQL, `.github/`, the vendor PSP sandbox or the AI SRE itself.
3. **Every revert write goes through `policy.check`.** These are the same rules as the agent's writes: no `main`, no workflows, the repo allowlist, dry-run. The one exception is `deploy/overlays/prod/`, which a revert may write only to restore the exact previous version (the image prod ran before).
4. **Temporal incidents are never reverted.** If investigated at all (`PROACTIVE_INVESTIGATE=true`), it's diagnosis only: the HITL policy webhook denies every write while one is in flight.
5. **Past incidents are hypotheses.** The playbook tells the agent to verify a matched root cause with its own queries before relying on it.
6. **Telemetry is untrusted data.** Log templates and past reports enter the prompt inside `<incident>` and `<skill>` blocks, under the playbook's existing rule 1.

## 9. Metrics

| Metric | Definition | Where it shows |
|---|---|---|
| MTTR | `resolved_ts - opened_ts` | `/incidents` KPI |
| Guarded rate | Share of resolved incidents whose fix left a guard | `/incidents` KPI |
| Recurrence count | Incidents with `recurrence_of` set | `/incidents` KPI |
| Revert precision | Reverts whose incident then resolved by that revert, out of all reverts | From `incident_events` |
| Match usefulness | Reports with `similar_incident` set, out of incidents with a match | From reports |

## 10. Configuration

| Env | Default | Meaning |
|---|---|---|
| `INCIDENTS_ENABLED` | `false` | Master switch. Off: behaviour is identical to `main`. |
| `AUTO_REVERT_PR` | `false` | Allow pattern-based revert PRs. |
| `PROACTIVE_INVESTIGATE` | `false` | Investigate new patterns seen outside a rollout, diagnosis only. |
| `PATTERN_SCAN_SECS` / `CHANGES_POLL_SECS` | `60` | Polling intervals. |
| `NEW_PATTERN_MIN_COUNT` | `20` | Errors before an out-of-rollout pattern opens an incident. |
| `GRAFANA_URL` / `_USERNAME` / `_PASSWORD` | in-cluster Grafana | Loki is read through the Grafana datasource proxy. |

No new GitHub permissions are needed. The revert uses the existing PAT (Contents and Pull requests: write).

## 11. Rollout plan

1. **Ship dark.** Merge with every flag off. Behaviour is unchanged and the PVC starts collecting the ledger.
2. **Turn on tracking only.** `INCIDENTS_ENABLED=true`: commits, patterns and incidents are tracked, and the agent gets context. Run the demo's two scenarios and check the `/incidents` page.
3. **Turn on reverts in staging rehearsal.** `AUTO_REVERT_PR=true` on the demo VM. Replay the JPY scenario twice; the second time should open a revert PR when the first incident's resolution was a revert.
4. **Proactive scanning** (`PROACTIVE_INVESTIGATE=true`) after the pattern noise level is understood.

Not part of the ArgoCon 2026-11-09 demo. The talk shows this as "what's next".

## 12. Open questions

- **Traffic baseline for slices.** Error logs alone can't tell "errors concentrate in sku-7/8/9" apart from "only sku-7/8/9 were sent". A per-attribute request baseline (from spans, or a request-count metric with bounded labels) would make slicing exact.
- **Auto-merge for reverts in staging?** Would shorten mitigation, but makes a bot the approver. Not in v1.
- **Similarity threshold tuning.** 0.8 for recurrence and revert is a guess. Tune it from fault-injection runs.
- **Fault-injection benchmark.** A suite of injected failures (bad config, leak, dependency timeout, schema change, expired cert) to score how often the agent finds the right root cause and how often its fix passes. That's how "adaptive" gets measured.
- **Multi-service.** Signatures and skills are per service. Cross-service incidents need a dependency map.
- **Concurrent diagnosis-only investigations** block writes for every in-flight investigation. Per-investigation policy needs AURA to pass a correlation id to the HITL webhook.

## 13. Test plan

Offline, in `ai-sre/tests/`: 29 tests, run with `cd ai-sre && pip install -r requirements.txt -r requirements-dev.txt && pytest`.

- Template mining, rescans that don't double-count, and pattern ids that are stable across restarts.
- Change classification and revert-safety rules.
- Similarity, matching, recurrence, and every lifecycle transition (staging fix on the same PR, prod fix that has to pass in prod, revert).
- The revert against a fake GitHub API: correct tree, refusals for main-moved-on, merge commits and protected paths, and dry-run.
- End to end through the FastAPI app: abort, then incident, then agent context, then mitigating, then staging pass (not resolved), then prod pass (guarded); recurrence matched to the past root cause; a revert opened when a matching past incident was resolved by revert.
- The policy webhook is unchanged, and diagnosis-only blocks writes.
- Read-only check against the real repo: `GITHUB_TOKEN=… python ai-sre/revert.py --plan <sha>`.
