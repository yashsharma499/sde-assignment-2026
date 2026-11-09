"""Durable state: SQLite (WAL) for decisions/governance/audit, plus an atomic pickle snapshot for the
ingest-derived aggregates.

Split of responsibilities (this is what makes restart safe):
  * Everything we DECIDED or SENT lives in SQLite and is committed before/after each side effect
    (intent -> submitted -> decided -> applied). It is never re-derived.
  * Everything we LEARNED from events (counters) lives in the snapshot together with the chunk watermark
    it is consistent with. On restart we load it and re-ingest chunks after the watermark. Ingestion is a
    pure function of the event stream, so replaying is idempotent.
"""
from __future__ import annotations

import json
import os
import pickle
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, sim_minute INTEGER, ts TEXT, workflow_id TEXT, kind TEXT,
  summary TEXT, detail TEXT, proposal_id TEXT, wall REAL);
CREATE INDEX IF NOT EXISTS audit_wf ON audit(workflow_id, sim_minute);
CREATE TABLE IF NOT EXISTS proposals (
  proposal_id TEXT PRIMARY KEY, workflow_id TEXT, path TEXT, kind TEXT, status TEXT, approval_id TEXT,
  base_version INTEGER, created_minute INTEGER, decided_minute INTEGER, applied_minute INTEGER,
  applied_version INTEGER, request TEXT, proposed_config TEXT, plan TEXT, reasons TEXT);
CREATE INDEX IF NOT EXISTS prop_wf ON proposals(workflow_id, status);
CREATE TABLE IF NOT EXISTS wf_state (workflow_id TEXT, key TEXT, v TEXT, PRIMARY KEY (workflow_id, key));
CREATE TABLE IF NOT EXISTS kpi_hourly (
  workflow_id TEXT, hour INTEGER, dials INTEGER, connected INTEGER, engaged INTEGER, flags INTEGER,
  PRIMARY KEY (workflow_id, hour));
CREATE TABLE IF NOT EXISTS configs (workflow_id TEXT, version INTEGER, actor TEXT, sim_minute INTEGER,
  changed_dims TEXT, proposal_id TEXT, approval_id TEXT, config TEXT, PRIMARY KEY (workflow_id, version));
CREATE TABLE IF NOT EXISTS loop_metrics (
  sim_minute INTEGER PRIMARY KEY, wall REAL, advance_s REAL, ingest_s REAL, decide_s REAL, events INTEGER,
  credits REAL, rss_mb REAL);
"""


class Store:
    def __init__(self, state_dir: str | Path):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.dir / "controller.db", isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.snapshot_path = self.dir / "aggregates.pkl"

    # ------------------------------------------------------------------ meta / per-workflow state
    def meta_get(self, k, default=None):
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def meta_set(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, json.dumps(v)))

    def wf_get(self, wid, key, default=None):
        r = self.db.execute("SELECT v FROM wf_state WHERE workflow_id=? AND key=?", (wid, key)).fetchone()
        return json.loads(r[0]) if r else default

    def wf_set(self, wid, key, v):
        self.db.execute("INSERT OR REPLACE INTO wf_state VALUES (?,?,?)", (wid, key, json.dumps(v)))

    # ------------------------------------------------------------------ audit
    def audit(self, sim_minute, ts, wid, kind, summary, detail=None, proposal_id=None):
        self.db.execute("INSERT INTO audit(sim_minute, ts, workflow_id, kind, summary, detail, proposal_id, wall)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (sim_minute, ts, wid, kind, summary, json.dumps(detail, default=str) if detail else None,
                         proposal_id, time.time()))

    # ------------------------------------------------------------------ proposals
    def prop_insert(self, rec: dict):
        cols = ["proposal_id", "workflow_id", "path", "kind", "status", "approval_id", "base_version",
                "created_minute", "request", "proposed_config", "plan"]
        vals = [rec.get(c) for c in cols]
        for i, c in enumerate(cols):
            if c in ("request", "proposed_config", "plan") and vals[i] is not None:
                vals[i] = json.dumps(vals[i])
        self.db.execute(f"INSERT INTO proposals({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)

    def prop_update(self, pid, **kw):
        sets, vals = [], []
        for k, v in kw.items():
            sets.append(f"{k}=?")
            vals.append(json.dumps(v) if isinstance(v, (dict, list)) else v)
        self.db.execute(f"UPDATE proposals SET {','.join(sets)} WHERE proposal_id=?", (*vals, pid))

    def props(self, wid=None, statuses=None, path=None):
        q, a = "SELECT * FROM proposals WHERE 1=1", []
        if wid:
            q += " AND workflow_id=?"
            a.append(wid)
        if statuses:
            q += f" AND status IN ({','.join('?' * len(statuses))})"
            a += list(statuses)
        if path:
            q += " AND path=?"
            a.append(path)
        q += " ORDER BY created_minute, rowid"
        cur = self.db.execute(q, a)
        names = [d[0] for d in cur.description]
        out = []
        for row in cur.fetchall():
            d = dict(zip(names, row))
            for c in ("request", "proposed_config", "plan", "reasons"):
                if d.get(c):
                    d[c] = json.loads(d[c])
            out.append(d)
        return out

    # ------------------------------------------------------------------ configs (our own copy of history)
    def config_record(self, wid, version, actor, minute, dims, pid, aid, cfg):
        self.db.execute("INSERT OR IGNORE INTO configs VALUES (?,?,?,?,?,?,?,?)",
                        (wid, version, actor, minute, json.dumps(dims), pid, aid,
                         json.dumps(cfg) if cfg is not None else None))

    # ------------------------------------------------------------------ snapshot
    def save_snapshot(self, obj):
        tmp = self.snapshot_path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            pickle.dump(obj, f, protocol=pickle.HIGHEST_PROTOCOL)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.snapshot_path)

    def load_snapshot(self):
        if not self.snapshot_path.exists():
            return None
        with open(self.snapshot_path, "rb") as f:
            return pickle.load(f)

    def wipe(self):
        self.db.close()
        for p in self.dir.glob("*"):
            if p.is_file():
                p.unlink()
        self.__init__(self.dir)
