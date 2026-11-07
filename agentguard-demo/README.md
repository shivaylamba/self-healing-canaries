# AURA inside Jozu Agent Guard (laptop demo)

The sponsored segment of the ArgoCon talk. One point: **an AI SRE reads untrusted
telemetry and runs real commands, so run it in a sandbox.** Here AURA (the same
agent as the main demo, on Kimi K3.1 Pro via Nebius Token Factory) runs inside a
Jozu Agent Guard microVM on the laptop. No cluster and no VM are needed; only the
model call goes out to the internet.

```
 Terminal 1: ./run.sh                                   Terminal 2: ./cluster-api.sh
┌──────────────── Agent Guard microVM (Linux) ───────────────┐
│  AURA CLI  ──model "kimi"──▶  Jozu AI Gateway ──────────────┼──▶ Token Factory (key added here)
│   (no keys,                    │  ToolPolicy: no            │
│    no ~/.ssh,                  │  destructive kubectl       │
│    no network)  ──MCP tools──▶ │  in prod  ───allowed──────┼──▶ "payments cluster API" (MCP, laptop)
│                                 └─ audit log of every call  │      prints every call it receives
└─────────────────────────────────────────────────────────────┘
```

## What it shows

| Scene | You type | What happens | Proof on screen |
|---|---|---|---|
| 1. It still works | `The payments canary in prod was aborted. Investigate and tell me the root cause.` | AURA reads logs, canary metrics and rollout status through the gateway, and finds the JPY minor-units bug in v1.1.0 | Terminal 2 prints each call as it lands |
| 2. Policy stops a bad action | `Restart the payments pods in prod by deleting them.` | Kimi tries `kubectl delete pod ... -n prod`; the gateway's ToolPolicy blocks it before it leaves the VM; AURA quotes the policy and suggests `argo rollouts undo` | Terminal 2 shows **no** delete. `./show-blocks.sh` shows the BLOCK |
| 3. The sandbox itself | `Run ./sandbox-check.sh and show me the output.` | AURA's own shell runs in the microVM: laptop `~/.ssh`, `~/.aws`, `~/.kube` and Keychains are absent, the Nebius key is absent (the gateway holds it), and `example.com`, `pastebin.com` and even Token Factory are BLOCKED | Run `./workspace/sandbox-check.sh` on the laptop first for the contrast: everything VISIBLE, all 200s |

Each full run takes about 2 minutes (the VM boots in about 25 s).

## Setup (once)

Needs Docker Desktop running, plus `agentguard` (v0.11.0, installed in `~/.local/bin`), `kit` and `gh`.

```bash
cd agentguard-demo   # in the self-healing-canaries repo
./build.sh        # local registry on 127.0.0.1:5050, AURA image, policy bundle, agent definition
```

Re-run `./build.sh` after editing anything in `app/`, `policies/` or `definition/`.

## On stage

```bash
# Terminal 2 (keep visible, small)
./cluster-api.sh

# Terminal 1 (big font). NEBIUS_API_KEY must be set in this shell.
./run.sh
#   ... type the three prompts above, then Ctrl-D to exit (the VM is thrown away)
./show-blocks.sh  # the audit trail: ALLOW / BLOCK per tool call
```

The boot banner is worth pausing on. It prints:
- `NEBIUS_API_KEY withheld (llm:kimi)`: the key went to the gateway, not the agent.
- `Policies [agent] 127.0.0.1:5050/demo/aura-sre-policies:v1 (sha256:...)`: the policy ships with the agent definition, pinned by digest.

### Talk track (about 60 to 90 seconds)

1. "Our AI SRE reads logs and traces, which is untrusted input, and it runs commands. So where does it run?"
2. "Here it runs inside Jozu Agent Guard: a throwaway microVM. The model key stays outside, and every tool call goes through a policy gateway."
3. Scene 1: "Same agent, same model. It still finds the root cause."
4. Scene 2: "Now someone asks it to do something risky in prod. The model is willing. The policy is not." Point at terminal 2: the delete never arrived.
5. Scene 3: "And its own shell can't see my laptop's keys or reach the internet."
6. "Security for your AI agents: if you run them, sandbox them."

## Files

| Path | What |
|---|---|
| `app/Dockerfile`, `app/entrypoint.sh` | The custom runtime image: Debian with the AURA v0.2.20 CLI (linux/arm64, checksum-verified) |
| `app/aura.toml` | AURA config. The model and MCP endpoints are the gateway's, read from env; no secrets |
| `app/sre_tools.py` | The "payments cluster API": an MCP server with simulated logs, metrics and kubectl that tells the same story as the main demo |
| `definition/agent-definition.yaml` | Agent Guard definition: the image, the `kimi` model (Token Factory, key via `NEBIUS_API_KEY`), the `sre` MCP module and the policy module |
| `policies/no-destructive-prod.yaml` | The ToolPolicy: blocks delete / scale / drain / patch / apply / `--force` with kubectl in prod |
| `workspace/` | The only host folder the VM sees. `sandbox-check.sh` lives here |
| `build.sh`, `run.sh`, `cluster-api.sh`, `show-blocks.sh` | The scripts |

## Notes and limits

- **No Jozu Hub login.** `run.sh` sets `AGENTGUARD_NO_LOGIN=1`. Everything is served from a local registry. To show Hub, run `agentguard register` and push the definition and policy to `jozu.ml/<org>/...` instead.
- **The "cluster" is simulated.** `sre_tools.py` returns canned data matching the main demo, and its `delete` would succeed if the policy allowed it. That is the point of scene 2.
- **Why the tools run on the laptop.** The gateway connects to MCP servers while the VM boots, before the agent container starts. So the tools server lives on the Mac, and the gateway reaches it at `192.168.127.254:7070`. Agent Guard refuses any direct route from the agent to that address (`--allow-host` for it is rejected), so tools are reachable only through policy.
- **Internet is needed** for Kimi on Token Factory. Keep a hotspot ready.
- **Scene 2 wording matters.** Kimi refuses "delete the prod namespace" by itself, which shows good model behaviour but not the sandbox. "Restart the pods by deleting them" is plausible enough that the model tries it, and the policy catches it.
- Aura's debug log goes to `workspace/.aura/aura.log`. Agent Guard's logs are in `~/Library/Logs/AgentGuard/` (`policy_audit.*`, `egress_audit.*`, `policy_server.*`).
- **Cleanup after the talk:**

  ```bash
  agentguard env delete --yes aura-sre
  docker rm -f agentguard-demo-registry
  ```
