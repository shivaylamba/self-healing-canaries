# Self-Healing Canaries

**ArgoCon NA: OTel-scoped Argo Rollouts analysis and an AI SRE that closes the loop.**

This talk follows up KubeCon India's *"From Flaky Builds to Self-Healing Deployments"*
([demo repo](https://github.com/chamodshehanka/kubecon-in-2026)). That talk used OTel as a CI/CD primitive. This one is about
**canary analysis**:

> Judge each canary on its own traffic, not the service-wide aggregate.
> 1% canary × 30% failing = **0.3%** service-wide, which an SLO alert never sees.

## What changed since KubeCon India

| KubeCon India | ArgoCon NA (this repo) |
|---|---|
| `build_id` threaded from CI into every span, log and metric | **No identity in the app.** Argo's `rollouts-pod-template-hash` is added by Prometheus relabel and the OTel Collector's `k8sattributes`. One level of cardinality, bounded by live ReplicaSets |
| CI build spans in Jaeger | Dropped. Runtime traces only |
| One gate | One **gate** plus two **dry-run** witnesses (service-wide error rate, canary vs stable p95) |
| AI SRE run by a script after rollback | Started by **Argo Rollouts notifications** (`on-rollout-aborted`) |
| Agent flips a config flag | Agent reads the diff and pushes a **code fix** to the PR, or opens a new PR |
| One cluster, CI patches the Rollout over SSH | **Two clusters** (staging, prod) with **Argo CD** + kustomize overlays. Build once, and the merge promotes |
| Live 3–5 min CI build on stage | Bad canaries are prebuilt and deploy instantly |
| No memory | A **release ledger**: which image passed or failed where |

## The story the demo tells

1. **PR #1** (inventory v2 + PSP v2) goes to staging. Its 1% canary fails (~30% on the canary,
   ~0.3% service-wide, and the canary is *faster*). Rollouts aborts.
2. The abort notification wakes the **AI SRE**. Grafana MCP shows the errors are all multi-warehouse SKUs.
   GitHub MCP reads the diff, and the agent **pushes a fix to PR #1**. CI rebuilds, and the same gate passes it.
3. A human merges, and **prod** gets the exact image. Prod's canary fails anyway, because only prod sends JPY.
4. The agent sees in the ledger that the **same image passed staging and failed prod**, finds the zero-decimal currency
   bug, and opens **PR #2**: the code fix plus JPY added to staging's traffic. In staging, the service-wide dry run
   goes red (old stable fails JPY) while the canary gate passes the fix. It merges, and prod goes green.

## Layout

```
apps/           Go services (payment, inventory, loadgen). Plain OTel SDK, no build identity
deploy/         kustomize base + staging/prod overlays (what Argo CD syncs)
platform/       kind configs, Gateway API, Rollouts plugin + notifications, OTel/Prom/Loki/Jaeger, Grafana, Argo CD
ai-sre/         FastAPI webhook + release ledger (MCP) + HITL policy; AURA agent config (aura.toml) + playbook
.github/        pr-image.yml (build once, promote via overlays), release.yml (bootstrap images), ci.yml
scripts/        up.sh, down.sh, build-local.sh, protect-main.sh, demo/*
demo/           PR #1 contents (the two latent bugs) + known-good fallback fixes
slides/         Slidev deck with speaker notes
docs/           DEMO-RUNBOOK.md
```

## Quick start

```bash
# prerequisites: docker, kind, kubectl, helm, gh, kubectl-argo-rollouts, perl
cp ai-sre/.env.example ai-sre/.env      # Nebius Token Factory key + fine-grained GitHub PAT (this repo only)
# GitHub: Actions → release → Run (v1.0.0), then make the scn-* packages public
scripts/protect-main.sh
scripts/up.sh
scripts/demo/status.sh
scripts/demo/prepare.sh                  # opens PR #1 (night before)
```

Then follow [docs/DEMO-RUNBOOK.md](docs/DEMO-RUNBOOK.md).

Slides: `cd slides && npm install && npm run dev`. For the Sched PDF, run `npm run export`.

## The three files that matter

- [`deploy/base/analysis-template.yaml`](deploy/base/analysis-template.yaml): one gate and two dry-run witnesses
- [`deploy/base/rollout.yaml`](deploy/base/rollout.yaml): `podTemplateHashValue`, Gateway API traffic routing, notifications
- [`ai-sre/playbook.md`](ai-sre/playbook.md): what the agent does, and what it must never do

## The AI SRE: AURA on Nebius Token Factory

The agent loop runs in [AURA](https://github.com/mezmo/aura), configured in [`ai-sre/aura.toml`](ai-sre/aura.toml):
the model (Kimi K3.1 Pro on Nebius Token Factory, via its OpenAI-compatible API), three MCP servers
(Grafana, GitHub, and the release ledger), a tool allowlist, and a HITL policy gate on every GitHub write.
The `ai-sre` container receives the Rollouts notification, hands it to AURA, and streams AURA's tool calls to the stage UI.
Change the model with `AI_SRE_MODEL` in `ai-sre/.env`.

## Safety model for the agent

1. **Tool filter**: AURA's `mcp_filter` exposes only read tools plus five GitHub writes. Merge, delete and branch-update tools are never exposed. Grafana MCP runs with `--disable-write`.
2. **Policy gate**: each of those writes is parked by AURA's `[hitl]` until `/hitl/policy` answers. It denies writes to `main`, to `.github/`, `deploy/overlays/prod/`, `demo/` and the PSP sandbox, and to any other repo.
3. **Token scope**: fine-grained PAT, one repo, contents + pull requests.
4. **Branch protection**: nothing lands on `main` without a PR.
5. **Same gate**: the agent's fix is judged by the same canary analysis as a human's.
6. **Human merge**: the merge is the promotion.

The agent has no Kubernetes credentials. Telemetry is treated as untrusted input.

## Incident memory (preview, off by default)

Beyond single rollbacks, the AI SRE can keep an incident memory: it tracks every commit on `main`,
mines error logs into templates per ReplicaSet, opens incidents with a signature, matches them
against past incidents, hands the agent a specialised skill (`ai-sre/skills/`), and, for a pattern it
has seen resolved by a revert, opens a revert PR deterministically while the agent prepares the real fix.
Humans still merge everything. Only failures attributed by the canary's pod-template-hash are ever
reverted; failures correlated by time alone are diagnosis only.

Design and safety rules: [`docs/prd-incident-memory.md`](docs/prd-incident-memory.md).
Switch it on with `INCIDENTS_ENABLED=true` in `ai-sre/k8s.yaml`; the incidents page is at `/incidents` in the demo UI.
Tests: `cd ai-sre && pip install -r requirements.txt -r requirements-dev.txt && pytest`.
