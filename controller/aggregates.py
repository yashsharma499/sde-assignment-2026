"""Event ingestion into dense numpy counters (C1, C2, C3).

Design:
  * One `Aggregates` object holds per-workflow cubes indexed by absolute sim-hour. Memory is fixed by the
    scenario length, not by event volume: ~7 MB per workflow for a 12-day run.
  * Lead features are joined at ingest time (`lead` event -> lead_id -> feature-cell code). The feature
    vocabulary is learned from events, never hard-coded from the simulator.
  * Ingest is chunk-atomic: the snapshot stores the aggregates and the last chunk folded into them.
    Replaying chunks after the watermark is idempotent because nothing outside this object is mutated.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

import numpy as np

FEATURE_NAMES = ("lang", "city_tier", "age_band", "value_band")
D0 = dt.datetime(2027, 1, 4)  # documented contract: D0 00:00 = 2027-01-04T00:00+05:30
MAX_CODES = 512


class CodeBook:
    """Feature tuple <-> dense integer code. Shared by every workflow (the 'one cohort object' basis)."""

    def __init__(self):
        self.index: dict[tuple, int] = {}
        self.rows: list[dict] = []

    def code(self, feats: dict) -> int:
        key = tuple(feats.get(f) for f in FEATURE_NAMES)
        c = self.index.get(key)
        if c is None:
            c = len(self.rows)
            if c >= MAX_CODES:
                raise RuntimeError("feature vocabulary larger than MAX_CODES")
            self.index[key] = c
            self.rows.append(dict(zip(FEATURE_NAMES, key)))
        return c

    def mask(self, where: dict) -> np.ndarray:
        """Boolean mask over codes for a cohort `where` clause (AND across features, OR within)."""
        m = np.zeros(MAX_CODES, dtype=bool)
        for i, row in enumerate(self.rows):
            m[i] = all(row[f] in vals for f, vals in where.items())
        return m

    def values(self, feat: str) -> list[str]:
        return sorted({r[feat] for r in self.rows})


class WfAgg:
    def __init__(self, wf: dict, base_hour: int, n_hours: int):
        o = wf["options"]
        self.wid = wf["workflow_id"]
        self.W, self.V, self.D, self.P = (list(o["welcome_message"]), list(o["voice"]), list(o["caller_id"]),
                                          list(o["prompt_version"]))
        self.Wi = {x: i for i, x in enumerate(self.W)}
        self.Vi = {x: i for i, x in enumerate(self.V)}
        self.Di = {x: i for i, x in enumerate(self.D)}
        self.Pi = {x: i for i, x in enumerate(self.P)}
        self.cadence_days = len(wf["cadence"])
        self.base = base_hour
        H = n_hours
        # connected/engaged by hour x feature-cell x welcome x voice
        self.exp = np.zeros((H, MAX_CODES, len(self.W), len(self.V), 2), dtype=np.int32)
        # dials/connected by hour x cell x attempt bucket (1, 2, 3+)
        self.pick = np.zeros((H, MAX_CODES, 3, 2), dtype=np.int32)
        # dials/connected by hour x caller id
        self.did = np.zeros((H, len(self.D), 2), dtype=np.int32)
        # connected/flagged/engaged by hour x prompt
        self.hal = np.zeros((H, len(self.P), 3), dtype=np.int32)
        self.lead_code: dict[str, int] = {}
        self.camp_leads: dict[str, list] = {}
        self.camp_day: dict[str, int] = {}
        self.camp_active: dict[str, np.ndarray] = {}
        self.person_code: dict[str, tuple[int, int]] = {}  # person -> (code, day last enrolled)
        self.last_batch_size = 0
        self.unknown_lead_calls = 0

    def hidx(self, hour_abs: int) -> int:
        return hour_abs - self.base

    def active_by_code(self) -> np.ndarray:
        out = np.zeros(MAX_CODES)
        for a in self.camp_active.values():
            out += a
        return out


class Aggregates:
    def __init__(self, workflows: list[dict], first_minute: int, end_minute: int):
        self.codes = CodeBook()
        self.base_hour = first_minute // 60 - 1
        self.n_hours = end_minute // 60 - self.base_hour + 2
        self.wf = {w["workflow_id"]: WfAgg(w, self.base_hour, self.n_hours) for w in workflows}
        self.last_chunk = -1
        self.last_event_id = 0
        self.events = 0
        self.max_minute = first_minute
        self.inbox: list[dict] = []   # config_changed / approval_decided / campaign_created, drained by the loop
        self._ts_cache: dict[str, int] = {}

    # ------------------------------------------------------------------ time
    def minute(self, ts: str) -> int:
        m = self._ts_cache.get(ts)
        if m is None:
            t = dt.datetime.fromisoformat(ts[:19])
            m = int((t - D0).total_seconds() // 60)
            if len(self._ts_cache) > 200_000:
                self._ts_cache.clear()
            self._ts_cache[ts] = m
        return m

    # ------------------------------------------------------------------ ingest
    def ingest(self, events, chunk_no: int):
        """Fold one chunk. Caller guarantees chunks are fed in order; replays are skipped by event_id."""
        if chunk_no <= self.last_chunk:
            return 0
        buf = defaultdict(lambda: defaultdict(list))
        n = 0
        last_id = self.last_event_id
        for ev in events:
            eid = ev["event_id"]
            if eid <= last_id:
                continue
            last_id = eid
            n += 1
            t = ev["type"]
            if t == "call":
                self._call(ev, buf)
            elif t == "lead":
                self._lead(ev)
            elif t == "campaign_created":
                self._campaign(ev)
                self.inbox.append(ev)
            else:  # config_changed, approval_decided
                self.inbox.append(ev)
        for wid, b in buf.items():
            self._flush(self.wf[wid], b)
        self.last_chunk = chunk_no
        self.last_event_id = last_id
        self.events += n
        return n

    def _lead(self, ev):
        a = self.wf[ev["workflow_id"]]
        c = self.codes.code(ev["features"])
        a.lead_code[ev["lead_id"]] = c
        cid = ev["campaign_id"]
        a.camp_leads.setdefault(cid, []).append(ev["lead_id"])
        a.camp_active.setdefault(cid, np.zeros(MAX_CODES, dtype=np.int32))[c] += 1
        a.person_code[ev["person_id"]] = (c, a.camp_day.get(cid, 0))

    def _campaign(self, ev):
        a = self.wf[ev["workflow_id"]]
        m = self.minute(ev["ts"])
        day = m // 1440
        a.camp_day[ev["campaign_id"]] = day
        a.last_batch_size = ev["leads"]
        # expire campaigns whose cadence is over (keeps lead map bounded to live campaigns)
        for cid, d in list(a.camp_day.items()):
            if day - d >= a.cadence_days + 1:
                for lid in a.camp_leads.pop(cid, []):
                    a.lead_code.pop(lid, None)
                a.camp_active.pop(cid, None)
                a.camp_day.pop(cid, None)
        for pid, (c, d) in list(a.person_code.items()):
            if day - d >= a.cadence_days + 1:
                del a.person_code[pid]

    def _call(self, ev, buf):
        a = self.wf[ev["workflow_id"]]
        m = self.minute(ev["ts"])
        if m > self.max_minute:
            self.max_minute = m
        h = m // 60 - a.base
        var = ev["variant"]
        conn = ev["connected"]
        b = buf[ev["workflow_id"]]
        d = a.Di[var["caller_id"]]
        b["did"].append((h, d, 0))
        if conn:
            b["did"].append((h, d, 1))
        code = a.lead_code.get(ev["lead_id"])
        if code is None:
            a.unknown_lead_calls += 1
        else:
            att = min(ev["attempt_no"], 3) - 1
            b["pick"].append((h, code, att, 0))
            if conn:
                b["pick"].append((h, code, att, 1))
        if not conn:
            return
        p = a.Pi[var["prompt_version"]]
        eng = ev["engaged"]
        b["hal"].append((h, p, 0))
        if ev["qa"] and ev["qa"]["hallucination_flag"]:
            b["hal"].append((h, p, 1))
        if eng:
            b["hal"].append((h, p, 2))
        if code is not None:
            w, v = a.Wi[var["welcome_message"]], a.Vi[var["voice"]]
            b["exp"].append((h, code, w, v, 0))
            if eng:
                b["exp"].append((h, code, w, v, 1))
                ca = a.camp_active.get(ev["campaign_id"])
                if ca is not None:
                    ca[code] -= 1

    @staticmethod
    def _flush(a: WfAgg, b):
        for name in ("did", "pick", "hal", "exp"):
            rows = b.get(name)
            if rows:
                idx = tuple(np.array(rows, dtype=np.int64).T)
                np.add.at(getattr(a, name), idx, 1)

    def drain_inbox(self):
        out, self.inbox = self.inbox, []
        return out
