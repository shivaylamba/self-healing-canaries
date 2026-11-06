"""Release ledger: which image passed or failed its canary, in which environment.

This is the memory the app layer doesn't carry. Telemetry stays low-cardinality
(pod-template-hash only); the ledger maps hash <-> image <-> commit <-> PR <-> verdict.
It is fed by Argo Rollouts notifications (on-rollout-completed / on-rollout-aborted).
"""
import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  env TEXT NOT NULL,
  image TEXT NOT NULL,
  git_sha TEXT,
  pod_hash TEXT,
  stable_hash TEXT,
  verdict TEXT NOT NULL,          -- passed | failed
  source_pr TEXT,
  source_branch TEXT
);
CREATE TABLE IF NOT EXISTS investigations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  env TEXT NOT NULL,
  image TEXT NOT NULL,
  status TEXT NOT NULL,           -- running | done | error | skipped
  event TEXT NOT NULL,
  steps TEXT NOT NULL DEFAULT '[]',
  report TEXT,
  error TEXT
);

-- ── Incident memory (docs/prd-incident-memory.md) ─────────────────────────
CREATE TABLE IF NOT EXISTS changes (
  sha TEXT PRIMARY KEY,
  ts REAL NOT NULL,               -- commit time
  author TEXT,
  message TEXT,
  pr TEXT,
  paths TEXT NOT NULL DEFAULT '[]',
  kinds TEXT NOT NULL DEFAULT '[]',  -- code | deploy | config | infra | test | docs | ci
  parent TEXT,
  revertable INTEGER NOT NULL DEFAULT 1,
  revert_reason TEXT
);
CREATE TABLE IF NOT EXISTS log_patterns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  template TEXT NOT NULL,         -- generalises as the miner merges lines
  error_type TEXT,
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pattern_hits (
  pattern_id INTEGER NOT NULL,
  env TEXT NOT NULL,
  pod_hash TEXT NOT NULL,
  window_start REAL NOT NULL,
  count INTEGER NOT NULL,
  slices TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (pattern_id, env, pod_hash, window_start)
);
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  opened_ts REAL NOT NULL,
  env TEXT NOT NULL,
  status TEXT NOT NULL,           -- open | mitigating | resolved | guarded | closed
  trigger TEXT NOT NULL,          -- rollout_aborted | new_pattern
  attribution TEXT NOT NULL,      -- hash | temporal
  image TEXT,
  pod_hash TEXT,
  git_sha TEXT,
  source_pr TEXT,
  signature TEXT NOT NULL DEFAULT '{}',
  matched_incident_id INTEGER,
  similarity REAL,
  recurrence_of INTEGER,          -- set when this repeats a resolved incident
  skill TEXT,
  investigation_id INTEGER,
  fix_pr TEXT,
  fix_image TEXT,
  revert_pr TEXT,
  guard_added INTEGER NOT NULL DEFAULT 0,
  resolution TEXT,                -- fix | revert | null
  resolved_ts REAL
);
CREATE TABLE IF NOT EXISTS incident_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  incident_id INTEGER NOT NULL,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}'
);
"""

_JSON_COLS = ("paths", "kinds", "slices", "signature", "detail")


def _decode(row: dict) -> dict:
    for k in _JSON_COLS:
        if k in row and isinstance(row[k], str):
            row[k] = json.loads(row[k])
    return row


class Ledger:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)

    def _exec(self, sql, args=()):
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    def _rows(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    # ── releases ────────────────────────────────────────────────────────────
    def record(self, ev: dict, verdict: str) -> None:
        last = self._rows(
            "SELECT verdict FROM releases WHERE env=? AND image=? AND pod_hash=? ORDER BY id DESC LIMIT 1",
            (ev["env"], ev["image"], ev.get("canaryHash")),
        )
        if last and last[0]["verdict"] == verdict:
            return  # duplicate notification
        self._exec(
            "INSERT INTO releases (ts, env, image, git_sha, pod_hash, stable_hash, verdict, source_pr, source_branch)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (time.time(), ev["env"], ev["image"], ev.get("gitSha"), ev.get("canaryHash"),
             ev.get("stableHash"), verdict, ev.get("sourcePR"), ev.get("sourceBranch")),
        )

    def releases(self, limit: int = 50) -> list[dict]:
        return self._rows("SELECT * FROM releases ORDER BY id DESC LIMIT ?", (limit,))

    def image_history(self, image: str) -> list[dict]:
        return self._rows("SELECT * FROM releases WHERE image=? ORDER BY id", (image,))

    # ── investigations ──────────────────────────────────────────────────────
    def recent_investigation(self, env: str, image: str, within_s: int = 1800) -> dict | None:
        rows = self._rows(
            "SELECT * FROM investigations WHERE env=? AND image=? AND status IN ('running','done') AND ts>?"
            " ORDER BY id DESC LIMIT 1",
            (env, image, time.time() - within_s),
        )
        return rows[0] if rows else None

    def start_investigation(self, ev: dict) -> int:
        cur = self._exec(
            "INSERT INTO investigations (ts, env, image, status, event) VALUES (?,?,?,?,?)",
            (time.time(), ev["env"], ev["image"], "running", json.dumps(ev)),
        )
        return cur.lastrowid

    def add_step(self, inv_id: int, step: dict) -> None:
        with self._lock:
            row = self._db.execute("SELECT steps FROM investigations WHERE id=?", (inv_id,)).fetchone()
            if row is None:
                return
            steps = json.loads(row["steps"]) + [{"t": time.time(), **step}]
            self._db.execute("UPDATE investigations SET steps=? WHERE id=?", (json.dumps(steps), inv_id))
            self._db.commit()

    def finish_investigation(self, inv_id: int, status: str, report: dict | None = None, error: str | None = None):
        self._exec(
            "UPDATE investigations SET status=?, report=?, error=? WHERE id=?",
            (status, json.dumps(report) if report else None, error, inv_id),
        )

    def investigations(self, limit: int = 10) -> list[dict]:
        rows = self._rows("SELECT * FROM investigations ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["event"] = json.loads(r["event"])
            r["steps"] = json.loads(r["steps"])
            r["report"] = json.loads(r["report"]) if r["report"] else None
        return rows

    # ── changes (commits on main) ───────────────────────────────────────────
    def upsert_change(self, c: dict) -> None:
        self._exec(
            "INSERT INTO changes (sha, ts, author, message, pr, paths, kinds, parent, revertable, revert_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(sha) DO UPDATE SET"
            " paths=excluded.paths, kinds=excluded.kinds, revertable=excluded.revertable,"
            " revert_reason=excluded.revert_reason, pr=COALESCE(excluded.pr, changes.pr)",
            (c["sha"], c["ts"], c.get("author"), c.get("message"), c.get("pr"),
             json.dumps(c.get("paths", [])), json.dumps(c.get("kinds", [])), c.get("parent"),
             1 if c.get("revertable", True) else 0, c.get("revert_reason")),
        )

    def change(self, sha: str) -> dict | None:
        """Look a change up by full or abbreviated SHA."""
        if not sha:
            return None
        rows = self._rows("SELECT * FROM changes WHERE sha LIKE ? ORDER BY ts DESC LIMIT 1", (sha + "%",))
        return _decode(rows[0]) if rows else None

    def recent_changes(self, since_ts: float, limit: int = 50) -> list[dict]:
        rows = self._rows("SELECT * FROM changes WHERE ts>=? ORDER BY ts DESC LIMIT ?", (since_ts, limit))
        return [_decode(r) for r in rows]

    def latest_change_ts(self) -> float | None:
        rows = self._rows("SELECT MAX(ts) AS t FROM changes")
        return rows[0]["t"] if rows else None

    # ── log patterns ────────────────────────────────────────────────────────
    def save_pattern(self, pid: int, template: str, error_type: str | None, ts: float) -> int:
        """Insert or update a pattern. `pid` is the miner's cluster id, so ids stay stable."""
        self._exec(
            "INSERT INTO log_patterns (id, template, error_type, first_seen, last_seen) VALUES (?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET template=excluded.template,"
            " first_seen=MIN(log_patterns.first_seen, excluded.first_seen),"
            " last_seen=MAX(log_patterns.last_seen, excluded.last_seen),"
            " error_type=COALESCE(log_patterns.error_type, excluded.error_type)",
            (pid, template, error_type, ts, ts),
        )
        return pid

    def all_patterns(self) -> list[dict]:
        return self._rows("SELECT * FROM log_patterns ORDER BY id")

    def add_pattern_hits(self, pattern_id: int, env: str, pod_hash: str, window_start: float,
                         count: int, slices: dict) -> None:
        self._exec(
            "INSERT INTO pattern_hits (pattern_id, env, pod_hash, window_start, count, slices) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(pattern_id, env, pod_hash, window_start) DO UPDATE SET count=excluded.count, slices=excluded.slices",
            (pattern_id, env, pod_hash, window_start, count, json.dumps(slices)),
        )

    def patterns_for_hash(self, env: str, pod_hash: str) -> list[dict]:
        """Every pattern seen on one ReplicaSet, with total count and merged slice counts."""
        rows = self._rows(
            "SELECT p.id, p.template, p.error_type, p.first_seen, h.count, h.slices"
            " FROM pattern_hits h JOIN log_patterns p ON p.id=h.pattern_id"
            " WHERE h.env=? AND h.pod_hash=? ORDER BY h.window_start",
            (env, pod_hash),
        )
        out: dict[int, dict] = {}
        for r in rows:
            p = out.setdefault(r["id"], {"id": r["id"], "template": r["template"], "error_type": r["error_type"],
                                         "first_seen": r["first_seen"], "count": 0, "slices": {}})
            p["count"] += r["count"]
            for key, vals in json.loads(r["slices"]).items():
                agg = p["slices"].setdefault(key, {})
                for v, n in vals.items():
                    agg[v] = agg.get(v, 0) + n
        return sorted(out.values(), key=lambda p: -p["count"])

    def pattern_hashes(self, pattern_id: int, env: str | None = None) -> list[str]:
        """The ReplicaSets (pod hashes) a pattern has ever been seen on."""
        sql, args = "SELECT DISTINCT pod_hash FROM pattern_hits WHERE pattern_id=?", [pattern_id]
        if env:
            sql += " AND env=?"
            args.append(env)
        return [r["pod_hash"] for r in self._rows(sql, tuple(args))]

    def hash_first_seen(self, env: str, pod_hash: str) -> float | None:
        rows = self._rows("SELECT MIN(window_start) AS t FROM pattern_hits WHERE env=? AND pod_hash=?",
                          (env, pod_hash))
        return rows[0]["t"] if rows else None

    # ── incidents ───────────────────────────────────────────────────────────
    _INCIDENT_COLS = (
        "opened_ts", "env", "status", "trigger", "attribution", "image", "pod_hash", "git_sha", "source_pr",
        "signature", "matched_incident_id", "similarity", "recurrence_of", "skill", "investigation_id",
        "fix_pr", "fix_image", "revert_pr", "guard_added", "resolution", "resolved_ts",
    )

    def create_incident(self, **f) -> int:
        f.setdefault("opened_ts", time.time())
        f.setdefault("status", "open")
        cols = [c for c in self._INCIDENT_COLS if c in f]
        vals = [json.dumps(f[c]) if c == "signature" else f[c] for c in cols]
        cur = self._exec(f"INSERT INTO incidents ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        return cur.lastrowid

    def update_incident(self, inc_id: int, **f) -> None:
        cols = [c for c in self._INCIDENT_COLS if c in f]
        if not cols:
            return
        vals = [json.dumps(f[c]) if c == "signature" else f[c] for c in cols]
        self._exec(f"UPDATE incidents SET {', '.join(c + '=?' for c in cols)} WHERE id=?", (*vals, inc_id))

    def incident(self, inc_id: int) -> dict | None:
        rows = self._rows("SELECT * FROM incidents WHERE id=?", (inc_id,))
        return _decode(rows[0]) if rows else None

    def incidents(self, limit: int = 50, status: str | None = None) -> list[dict]:
        if status:
            rows = self._rows("SELECT * FROM incidents WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit))
        else:
            rows = self._rows("SELECT * FROM incidents ORDER BY id DESC LIMIT ?", (limit,))
        return [_decode(r) for r in rows]

    def active_incident(self, env: str, image: str | None = None, pod_hash: str | None = None) -> dict | None:
        """The newest unresolved incident for this env and image (or ReplicaSet)."""
        sql = "SELECT * FROM incidents WHERE env=? AND status IN ('open','mitigating')"
        args: list = [env]
        if image:
            sql += " AND image=?"
            args.append(image)
        if pod_hash:
            sql += " AND pod_hash=?"
            args.append(pod_hash)
        rows = self._rows(sql + " ORDER BY id DESC LIMIT 1", tuple(args))
        return _decode(rows[0]) if rows else None

    def incidents_for_pr(self, pr: str) -> list[dict]:
        rows = self._rows(
            "SELECT * FROM incidents WHERE status IN ('open','mitigating') AND (fix_pr=? OR revert_pr=? OR source_pr=?)"
            " ORDER BY id", (pr, pr, pr))
        return [_decode(r) for r in rows]

    def closed_incidents(self, limit: int = 500) -> list[dict]:
        """Incidents with a known outcome: the memory new incidents are matched against."""
        rows = self._rows(
            "SELECT * FROM incidents WHERE status IN ('resolved','guarded','closed') ORDER BY id DESC LIMIT ?",
            (limit,))
        return [_decode(r) for r in rows]

    def add_incident_event(self, inc_id: int, kind: str, detail: dict | None = None) -> None:
        self._exec("INSERT INTO incident_events (incident_id, ts, kind, detail) VALUES (?,?,?,?)",
                   (inc_id, time.time(), kind, json.dumps(detail or {}, default=str)))

    def incident_events(self, inc_id: int) -> list[dict]:
        return [_decode(r) for r in self._rows(
            "SELECT * FROM incident_events WHERE incident_id=? ORDER BY id", (inc_id,))]

    def investigation(self, inv_id: int) -> dict | None:
        rows = self._rows("SELECT * FROM investigations WHERE id=?", (inv_id,))
        if not rows:
            return None
        r = rows[0]
        r["event"] = json.loads(r["event"])
        r["steps"] = json.loads(r["steps"])
        r["report"] = json.loads(r["report"]) if r["report"] else None
        return r
