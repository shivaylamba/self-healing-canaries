"""Guardrail 2: the write policy. AURA's HITL webhook (/hitl/policy) asks it about every
GitHub write the agent makes, and the incident system's deterministic revert asks it too.
"""
from __future__ import annotations

from agent import DRY_RUN, GITHUB_REPO

PROTECTED_BRANCHES = {"main", "master"}
PROTECTED_PATHS = (".github/", "deploy/overlays/prod/", "demo/", "apps/payment/pspsandbox/")
BRANCH_TOOLS = ("push_files", "create_or_update_file", "create_branch", "create_pull_request")


def check(tool: str, a: dict, allow_paths: tuple[str, ...] = (), dry_run: bool | None = None,
          repo_name: str | None = None) -> str | None:
    """Return a denial reason, or None to allow.

    allow_paths: protected prefixes this caller may write anyway. Only the revert uses it, for
    deploy/overlays/prod/, and only to restore a file's exact previous content."""
    if DRY_RUN if dry_run is None else dry_run:
        return "dry-run mode: GitHub writes are disabled"
    expected = repo_name or GITHUB_REPO
    repo = f"{a.get('owner', '')}/{a.get('repo', '')}"
    if a.get("owner") and repo.lower() != expected.lower():
        return f"only {expected} may be changed, not {repo}"
    branch = a.get("branch") if tool != "create_pull_request" else a.get("head")
    if tool in BRANCH_TOOLS:
        if not branch:
            return "a target branch is required"
        if branch.split(":")[-1] in PROTECTED_BRANCHES:
            return f"writes to {branch} are not allowed; push to a PR branch"
    paths = [f.get("path", "") for f in a.get("files", []) if isinstance(f, dict)]
    if a.get("path"):
        paths.append(a["path"])
    for path in paths:
        p = path.lstrip("/")
        if p.startswith(PROTECTED_PATHS) and not p.startswith(allow_paths or ("\0",)):
            return f"{path} is a protected path"
    return None
