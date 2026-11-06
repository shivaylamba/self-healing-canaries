"""Log pattern mining (PRD FR2).

Error logs are turned into templates ("inventory <*> for <*>: missing available field")
with a small Drain-style miner, and every template is counted per ReplicaSet
(rollouts_pod_template_hash), per scan window, with the business attributes the
errors concentrate in (currency, region, SKU).

Because the hash is on every log line, "this pattern only exists on the canary's
ReplicaSet" is a fact, not a correlation by time. That is what lets an incident be
attributed to a commit, and what makes reverting on a pattern safe.
"""
from __future__ import annotations

import logging
import re
import time
from collections import Counter, defaultdict

import httpx

log = logging.getLogger("ai-sre.patterns")

WILD = "<*>"
SLICE_KEYS = ("checkout_currency", "checkout_region", "checkout_sku")
HASH_LABEL = "rollouts_pod_template_hash"

_QUOTED = re.compile(r"(\"[^\"]*\"|'[^']*'|`[^`]*`)")
_SPLIT = re.compile(r"[\s,;=()\[\]{}]+")
_HAS_DIGIT = re.compile(r"\d")


def tokenize(line: str) -> list[str]:
    """Split a log message into tokens and mask the obviously variable ones."""
    line = _QUOTED.sub(WILD, line.strip())
    out = []
    for tok in _SPLIT.split(line):
        if not tok:
            continue
        tok = tok.rstrip(":.")
        out.append(WILD if (_HAS_DIGIT.search(tok) or tok == WILD) else tok)
    return out


class _Cluster:
    __slots__ = ("id", "tokens", "size")

    def __init__(self, cid: int, tokens: list[str]):
        self.id, self.tokens, self.size = cid, tokens, 0

    @property
    def template(self) -> str:
        return " ".join(self.tokens)


class TemplateMiner:
    """Drain, simplified: clusters are grouped by (token count, first token); a line joins
    the most similar cluster above `threshold`, and positions that differ become <*>."""

    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        self._groups: dict[tuple[int, str], list[_Cluster]] = defaultdict(list)
        self._next = 1

    def seed(self, cid: int, template: str) -> None:
        """Load a template persisted earlier so ids stay stable across restarts."""
        tokens = template.split(" ")
        self._groups[(len(tokens), tokens[0] if tokens else "")].append(_Cluster(cid, tokens))
        self._next = max(self._next, cid + 1)

    @staticmethod
    def _sim(a: list[str], b: list[str]) -> float:
        same = sum(1 for x, y in zip(a, b) if x == y and x != WILD)
        fixed = sum(1 for x in a if x != WILD) or 1
        return same / fixed

    def template(self, cid: int) -> str:
        for group in self._groups.values():
            for c in group:
                if c.id == cid:
                    return c.template
        raise KeyError(cid)

    def add(self, line: str) -> tuple[int, str, bool]:
        """Returns (cluster id, template, whether the template changed or is new)."""
        tokens = tokenize(line)
        if not tokens:
            tokens = [WILD]
        key = (len(tokens), tokens[0])
        best, best_sim = None, -1.0
        for c in self._groups[key]:
            s = self._sim(c.tokens, tokens)
            if s > best_sim:
                best, best_sim = c, s
        if best is None or best_sim < self.threshold:
            c = _Cluster(self._next, tokens)
            self._next += 1
            c.size = 1
            self._groups[key].append(c)
            return c.id, c.template, True
        merged = [x if x == y else WILD for x, y in zip(best.tokens, tokens)]
        changed = merged != best.tokens
        best.tokens = merged
        best.size += 1
        return best.id, best.template, changed


def summarize(entries: list[dict], miner: TemplateMiner, window_s: int = 60) -> dict[tuple, dict]:
    """entries: [{"ts", "line", "labels"}] ->
    {(cluster id, pod hash, window start): {template, error_type, count, slices}}.

    Hits are bucketed by each line's own timestamp, so scanning an overlapping range again
    overwrites a bucket with the same count instead of counting it twice."""
    out: dict[tuple, dict] = {}
    for e in entries:
        labels = e.get("labels", {})
        h = labels.get(HASH_LABEL)
        if not h:
            continue
        cid, template, _ = miner.add(e["line"])
        ts = e.get("ts", 0.0)
        bucket = ts - (ts % window_s)
        agg = out.setdefault((cid, h, bucket), {"error_type": labels.get("error_type"), "count": 0,
                                                "slices": {k: Counter() for k in SLICE_KEYS}})
        agg["count"] += 1
        for k in SLICE_KEYS:
            if labels.get(k):
                agg["slices"][k][labels[k]] += 1
    for (cid, _h, _b), agg in out.items():
        agg["template"] = miner.template(cid)  # final (most general) form
        agg["slices"] = {k: dict(v) for k, v in agg["slices"].items() if v}
    return out


def dominant_slice(slices: dict[str, dict[str, int]], min_share: float = 0.8, max_values: int = 3) -> dict[str, str]:
    """Attributes whose top few values (at most `max_values`) carry at least `min_share` of a
    pattern's errors: {"checkout_currency": "JPY"} or {"checkout_sku": "sku-7|sku-8|sku-9"}."""
    dom = {}
    for key, vals in slices.items():
        total = sum(vals.values())
        if not total:
            continue
        covered, picked = 0, []
        for v, n in sorted(vals.items(), key=lambda kv: (-kv[1], kv[0]))[:max_values]:
            covered += n
            picked.append(v)
            if covered / total >= min_share:
                # Several values only count as a slice when some observed values are left out;
                # otherwise it's just "every value that errored". (A real traffic baseline would
                # do better: see the PRD's open questions.)
                if len(picked) == 1 or len(picked) < len(vals):
                    dom[key] = "|".join(sorted(picked))
                break
    return dom


def new_on_canary(ledger, env: str, canary_hash: str, stable_hash: str | None) -> list[dict]:
    """Patterns seen on the canary's ReplicaSet and never on the stable one: hash-attributed."""
    stable = {p["id"] for p in ledger.patterns_for_hash(env, stable_hash)} if stable_hash else set()
    return [p for p in ledger.patterns_for_hash(env, canary_hash) if p["id"] not in stable]


class LokiReader:
    """Reads error logs through Grafana's datasource proxy, so prod's Loki is reachable
    the same way the Grafana MCP server reaches it."""

    def __init__(self, grafana_url: str, user: str, password: str, client: httpx.AsyncClient | None = None,
                 query: str = '{service_name="payment"} | severity_text="ERROR"'):
        self.query = query
        self.client = client or httpx.AsyncClient(base_url=grafana_url, auth=(user, password), timeout=20)

    async def errors(self, env: str, start: float, end: float, limit: int = 2000) -> list[dict]:
        r = await self.client.get(
            f"/api/datasources/proxy/uid/loki-{env}/loki/api/v1/query_range",
            params={"query": self.query, "start": int(start * 1e9), "end": int(end * 1e9),
                    "limit": limit, "direction": "backward"},
        )
        r.raise_for_status()
        out = []
        # Without categorize-labels, Loki merges structured metadata (the hash, error_type,
        # checkout_*) into each stream's labels.
        for stream in r.json().get("data", {}).get("result", []):
            labels = stream.get("stream", {})
            for ts_ns, line in stream.get("values", []):
                out.append({"ts": int(ts_ns) / 1e9, "line": line, "labels": labels})
        return out


class PatternScanner:
    """One scan = read the last window of error logs for an env, mine, persist hits."""

    def __init__(self, ledger, reader: LokiReader, envs: tuple[str, ...] = ("staging", "prod"),
                 window_s: int = 60):
        self.ledger, self.reader, self.envs, self.window_s = ledger, reader, envs, window_s
        self.miner = TemplateMiner()
        for p in ledger.all_patterns():
            self.miner.seed(p["id"], p["template"])

    async def scan(self, env: str, now: float | None = None, lookback_s: int | None = None) -> list[dict]:
        """Read [now - lookback, now] (default: two windows) and persist. Returns patterns
        that are new to the whole system."""
        now = now or time.time()
        entries = await self.reader.errors(env, now - (lookback_s or 2 * self.window_s), now)
        return self.ingest(env, entries)

    def ingest(self, env: str, entries: list[dict]) -> list[dict]:
        known = {p["id"] for p in self.ledger.all_patterns()}
        new: dict[int, dict] = {}
        for (cid, h, bucket), agg in summarize(entries, self.miner, self.window_s).items():
            pid = self.ledger.save_pattern(cid, agg["template"], agg["error_type"], bucket)
            self.ledger.add_pattern_hits(pid, env, h, bucket, agg["count"], agg["slices"])
            if pid not in known:
                if pid in new:
                    new[pid]["count"] += agg["count"]
                else:
                    new[pid] = {"id": pid, "env": env, "pod_hash": h, **agg}
        return list(new.values())
