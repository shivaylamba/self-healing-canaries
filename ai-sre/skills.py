"""Specialised skills (PRD FR5).

A skill is a markdown playbook for one class of failure, with front-matter that says
when it applies. The router scores every skill against the incident's signature and
the agent gets the best one appended to its task, instead of one giant prompt that
has to know every failure mode.

Front-matter (between --- lines), flat `key: value`, lists as [a, b]:
    name, summary
    error_types        error.type values from the logs           (+3 each)
    slice_keys         attributes errors concentrate in          (+2 each)
    template_regex     regex over log templates                  (+2 if any matches)
    path_globs         globs over the offending change's files   (+1 each file match, max 2)
    path_kinds         code | config | deploy | infra ...        (+1 each)
    revert_preference  first | never | fix_first (default)
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

SKILLS_DIR = Path(__file__).parent / "skills"


def _value(raw: str):
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        return [x.strip().strip("'\"") for x in raw[1:-1].split(",") if x.strip()]
    return raw.strip("'\"")


def parse(text: str) -> dict:
    meta: dict = {}
    body = text
    if text.startswith("---"):
        _, fm, body = text.split("---", 2)
        for line in fm.strip().splitlines():
            if ":" in line and not line.lstrip().startswith("#"):
                k, v = line.split(":", 1)
                meta[k.strip()] = _value(v)
    meta.setdefault("revert_preference", "fix_first")
    meta["body"] = body.strip()
    return meta


def load(directory: Path = SKILLS_DIR) -> list[dict]:
    return [parse(p.read_text()) for p in sorted(directory.glob("*.md"))]


def score(skill: dict, sig: dict) -> int:
    s = 0
    s += 3 * len(set(skill.get("error_types", [])) & set(sig.get("error_types", [])))
    s += 2 * len(set(skill.get("slice_keys", [])) & set((sig.get("slice") or {}).keys()))
    if skill.get("template_regex"):
        rx = re.compile(skill["template_regex"], re.I)
        if any(rx.search(t) for t in sig.get("templates", [])):
            s += 2
    globs = skill.get("path_globs", [])
    s += min(2, sum(1 for f in sig.get("files", []) if any(fnmatch.fnmatch(f, g) for g in globs)))
    s += len(set(skill.get("path_kinds", [])) & set(sig.get("path_kinds", [])))
    return s


def select(sig: dict, skills: list[dict] | None = None) -> tuple[dict, int]:
    """Best skill for a signature; `generic` when nothing scores."""
    skills = skills if skills is not None else load()
    ranked = sorted(((score(sk, sig), sk) for sk in skills if sk["name"] != "generic"),
                    key=lambda x: -x[0])
    if ranked and ranked[0][0] > 0:
        return ranked[0][1], ranked[0][0]
    generic = next((sk for sk in skills if sk["name"] == "generic"), {"name": "generic", "body": "",
                                                                       "revert_preference": "fix_first"})
    return generic, 0


def get(name: str, skills: list[dict] | None = None) -> dict | None:
    return next((sk for sk in (skills if skills is not None else load()) if sk["name"] == name), None)
