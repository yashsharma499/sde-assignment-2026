"""The always-on loop: ingest -> reconcile governance -> decide -> checkpoint -> advance.

Side effects are written as intents to SQLite *before* the HTTP call and confirmed after it, so a crash at
any line leaves enough to reconcile with GET /approvals and GET /workflows/{id}/config/history.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import os
import time

from . import params as P
from . import policy as pol
from .aggregates import Aggregates
from .client import SimClient
from .stats import tv_distance
from .store import Store

log = logging.getLogger("controller")
OPEN_APPROVAL = ("intent", "pending", "approved", "applying")


def _norm_cfg(cfg: dict) -> str:
    """Canonical form for equality checks (weights normalised and rounded)."""
    c = copy.deepcopy(cfg)
    for k, dims in c.get("allocations", {}).items():
        for d, w in dims.items():
            s = sum(w.values()) or 1
            dims[d] = {o: round(v / s, 4) for o, v in sorted(w.items()) if v / s > 1e-6}
    return json.dumps(c, sort_keys=True)


def _rss_mb() -> float:
    try:
        import resource  # noqa
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:
        try:
            import psutil  # optional
            return psutil.Process(os.getpid()).memory_info().rss / 2**20
        except Exception:
            return -1.0


class Controller:
    def __init__(self, url: str, state_dir: str, stop_after_minute: int | None = None):
        self.c = SimClient(url)
        self.store = Store(state_dir)
        self.stop_after = stop_after_minute
        st = self._get("/sim/state")
        self.sim = st
        self.wfs = {w["workflow_id"]: w for w in self._get("/workflows")}
        fp = {"scenario": st["scenario"], "workflows": sorted(self.wfs)}
        last = self.store.meta_get("last_sim_minute")
        if self.store.meta_get("fingerprint") != fp or (last is not None and st["sim_minute"] < last):
            log.info("new world (%s) - starting from empty state", st["scenario"])
            self.store.wipe()
            self.store.meta_set("fingerprint", fp)
        snap = self.store.load_snapshot()
        self.agg: Aggregates | None = snap["agg"] if snap else None
        if snap:
            log.info("resumed snapshot at chunk %s (event %s)", self.agg.last_chunk, self.agg.last_event_id)
            self.store.audit(st["sim_minute"], st["ts"], None, "restart",
                             f"controller restarted at {st['ts']}; snapshot chunk {self.agg.last_chunk}, "
                             f"replaying newer chunks", {"last_chunk": self.agg.last_chunk})
        self.live: dict[str, dict] = {}
        self.credits = st["credits_remaining"]
        self.tick_n = 0

    # ================================================================== http helpers
    def _get(self, path, **kw):
        code, body = self.c.get(path, **kw)
        if code != 200:
            raise RuntimeError(f"GET {path}: {code} {body}")
        return body

    @property
    def now(self) -> int:
        return self.sim["sim_minute"]

    def ts(self) -> str:
        return self.sim["ts"]

    def audit(self, wid, kind, summary, detail=None, pid=None):
        self.store.audit(self.now, self.ts(), wid, kind, summary, detail, pid)
        log.info("[%s] %s %s: %s", self.ts()[5:16], wid or "-", kind, summary)

    # ================================================================== ingest
    def catch_up(self) -> int:
        chunks = []
        for kind in ("history", "events"):
            after = self.agg.last_chunk if self.agg else -1
            chunks += self._get("/events/chunks", kind=kind, after=after)
        chunks.sort(key=lambda c: c["chunk"])
        n = 0
        for ch in chunks:
            evs = list(self.c.chunk_lines(ch["chunk"]))
            if self.agg is None:
                self.agg = self._new_agg(evs)
            n += self.agg.ingest(evs, ch["chunk"])
        return n

    def _new_agg(self, evs) -> Aggregates:
        import datetime as dt
        from .aggregates import D0
        t = dt.datetime.fromisoformat(evs[0]["ts"][:19])
        first = int((t - D0).total_seconds() // 60)
        return Aggregates(list(self.wfs.values()), first, self.sim["end_minute"])

    def checkpoint(self):
        self.store.save_snapshot({"agg": self.agg})

    # ================================================================== bootstrap (C3)
    def bootstrap(self):
        if self.store.meta_get("bootstrapped"):
            return
        now_h = self.now // 60
        for wid, wf in self.wfs.items():
            a = self.agg.wf[wid]
            cfg = self._get(f"/workflows/{wid}/config")
            self.store.config_record(wid, cfg["version"], cfg["actor"], self.now, [], None, None, cfg["config"])
            for h in self._get(f"/workflows/{wid}/config/history"):
                self.store.config_record(wid, h["version"], h["actor"], None, h["changed_dims"], h["proposal_id"],
                                         h["approval_id"], None)
            # caller-ID health from history
            st, notes = pol.did_assess(a, now_h, {})
            self.store.wf_set(wid, "did", st)
            # hallucination baseline: flags / connects over history, all prompts that ran
            n = int(a.hal[:, :, 0].sum())
            f = int(a.hal[:, :, 1].sum())
            base = max(f / n if n else 0.01, 0.005)
            self.store.wf_set(wid, "hal_base", base)
            self.store.wf_set(wid, "prompt_since", {"prompt": cfg["config"]["prompt_version"], "hour": now_h - 24 * 30})
            self.store.wf_set(wid, "exp", pol.new_exp_state(cfg["config"]))
            pick = a.pick.sum(axis=(0, 1, 2))
            eng = a.exp[..., 1].sum()
            conn = a.exp[..., 0].sum()
            self.store.wf_set(wid, "baseline", {"pickup": pick[1] / max(pick[0], 1), "engagement": eng / max(conn, 1),
                                                 "dials": int(pick[0])})
            self.audit(wid, "bootstrap",
                       f"history: {int(pick[0])} dials, pickup {pick[1] / max(pick[0], 1):.3f}, engagement "
                       f"{eng / max(conn, 1):.3f}, flag rate {base:.4f}; caller IDs: "
                       + ", ".join(f"{d}={s['status']}" for d, s in st.items()),
                       {"did": st, "objective": wf["objective"], "governance": wf["governance"]})
        self.store.meta_set("bootstrapped", True)

    # ================================================================== governance (C9)
    def policy_of(self, wid):
        return self.wfs[wid]["governance"]

    def auto_allowed(self, wid, dims) -> bool:
        aa = self.policy_of(wid)["auto_apply"]
        touched = set(self.store.wf_get(wid, "supervisor_dims", []))
        return bool(aa.get("enabled")) and all(d in aa.get("allowed_dims", []) and d not in touched for d in dims)

    def approvals_last_24h(self, wid) -> int:
        rows = self.store.props(wid, path="approval")
        return sum(1 for r in rows if r["approval_id"] and r["created_minute"] > self.now - 1440)

    def approval_in_flight(self, wid) -> bool:
        return any(True for _ in self.store.props(wid, statuses=OPEN_APPROVAL, path="approval"))

    def routine_cap(self, wid) -> int:
        cap = self.policy_of(wid)["max_requests_per_24h"]
        pref = P.TENANT_PREFS.get(self.wfs[wid]["tenant_id"], {})
        return max(1, min(cap - P.FATIGUE_RESERVE_FOR_URGENT, pref.get("max_routine_per_24h", cap)))

    def _pid(self, wid, kind):
        n = self.store.meta_get("pid_seq", 0) + 1
        self.store.meta_set("pid_seq", n)
        return f"{wid}:{kind}:{self.now}:{n}"

    def submit_approval(self, wid, kind, cfg, req: dict, plan: dict) -> str | None:
        live = self.live[wid]
        pid = self._pid(wid, kind)
        body = {"proposal_id": pid, "workflow_id": wid, "base_version": live["version"], "proposed_config": cfg,
                "expires_in_hours": self.policy_of(wid)["proposal_ttl_hours"], **req}
        self.store.prop_insert({"proposal_id": pid, "workflow_id": wid, "path": "approval", "kind": kind,
                                "status": "intent", "base_version": live["version"], "created_minute": self.now,
                                "request": req, "proposed_config": cfg, "plan": plan})
        code, resp = self.c.post("/approvals", body)
        if code == 200:
            self.store.prop_update(pid, status="pending", approval_id=resp["approval_id"])
            self.audit(wid, "proposal_submitted", f"{kind}: {req['change_summary']} -> {resp['approval_id']}",
                       {"request": req, "approval": resp, "plan_decisions": plan.get("decisions")}, pid)
            return pid
        self.store.prop_update(pid, status="failed", reasons=[code, resp])
        self.audit(wid, "proposal_failed", f"{kind}: HTTP {code} {resp}", {"request": req}, pid)
        return None

    def submit_auto(self, wid, kind, cfg, rationale: str, evidence: dict, plan: dict) -> bool:
        live = self.live[wid]
        pid = self._pid(wid, kind)
        body = {"proposal_id": pid, "base_version": live["version"], "proposed_config": cfg,
                "rationale": rationale, "evidence": evidence}
        self.store.prop_insert({"proposal_id": pid, "workflow_id": wid, "path": "auto", "kind": kind,
                                "status": "intent", "base_version": live["version"], "created_minute": self.now,
                                "request": {"rationale": rationale, "evidence": evidence}, "proposed_config": cfg,
                                "plan": plan})
        code, resp = self.c.put(f"/workflows/{wid}/config", body)
        if code == 200:
            self.store.prop_update(pid, status="applied", applied_minute=self.now, applied_version=resp["version"])
            self.audit(wid, "auto_applied", f"{kind} v{resp['version']} dims={resp['changed_dims']}: {rationale}",
                       {"evidence": evidence, "plan_decisions": plan.get("decisions")}, pid)
            self._after_apply(wid, plan, resp["version"])
            return True
        self.store.prop_update(pid, status="auto_rejected" if code == 403 else "failed", reasons=[code, resp])
        self.audit(wid, "auto_apply_refused", f"{kind}: HTTP {code} {resp}", {"rationale": rationale}, pid)
        return False

    def poll_governance(self, wid):
        for p in self.store.props(wid, statuses=("pending", "approved", "applying"), path="approval"):
            code, ap = self.c.get(f"/approvals/{p['approval_id']}")
            if code != 200:
                continue
            st = ap["status"]
            if st == "pending":
                continue
            if st in ("rejected", "expired", "lapsed"):
                self.store.prop_update(p["proposal_id"], status=st, reasons=ap.get("reasons"),
                                       decided_minute=self.now)
                self.audit(wid, f"approval_{st}", f"{p['kind']} {p['approval_id']} {st}: {ap.get('reasons')}",
                           {"approval": ap}, p["proposal_id"])
                self._on_closed(wid, p, st)
            elif st == "applied":
                if p["status"] != "applied":
                    self.store.prop_update(p["proposal_id"], status="applied", applied_minute=self.now)
                    self._after_apply(wid, p["plan"] or {}, self._get(f"/workflows/{wid}/config")["version"])
            elif st == "approved":
                self.store.prop_update(p["proposal_id"], status="applying", decided_minute=self.now)
                code, resp = self.c.post(f"/workflows/{wid}/apply", {"approval_id": p["approval_id"]})
                if code == 200:
                    self.store.prop_update(p["proposal_id"], status="applied", applied_minute=self.now,
                                           applied_version=resp["version"])
                    self.audit(wid, "applied", f"{p['kind']} {p['approval_id']} approved {ap.get('decided_at', '')[5:16]}"
                               f" -> v{resp['version']} dims={resp['changed_dims']}: "
                               f"{(p['request'] or {}).get('change_summary')}",
                               {"approval": ap, "version": resp["version"]}, p["proposal_id"])
                    self.live[wid] = self._get(f"/workflows/{wid}/config")
                    self._after_apply(wid, p["plan"] or {}, resp["version"])
                elif code == 409 and resp.get("error") == "stale_base_version":
                    self.store.prop_update(p["proposal_id"], status="stale", reasons=[resp])
                    self.audit(wid, "apply_stale", f"{p['approval_id']} approved but config moved to "
                               f"v{resp['detail']['current_version']} (supervisor edit?) - will re-propose on the new base",
                               resp, p["proposal_id"])
                    self._on_closed(wid, p, "stale")
                elif code == 410:
                    self.store.prop_update(p["proposal_id"], status="lapsed")
                    self.audit(wid, "approval_lapsed", f"{p['approval_id']} lapsed", resp, p["proposal_id"])
                    self._on_closed(wid, p, "lapsed")
                else:
                    self.store.prop_update(p["proposal_id"], status="approved")
                    log.warning("apply %s -> %s %s", p["approval_id"], code, resp)

    def _on_closed(self, wid, p, status):
        """Expired == rejected (stakeholders/05). Nothing from the plan is committed; we re-plan from data."""
        if p["kind"] == "incident":
            inc = self.store.wf_get(wid, "incident", {})
            if inc.get("proposal_id") == p["proposal_id"]:
                inc["status"] = "open"
                inc["proposal_id"] = None
                self.store.wf_set(wid, "incident", inc)
        elif p["kind"] == "routine":
            d = self.now // 1440
            self.store.wf_set(wid, "routine_retry_day", d)

    def _after_apply(self, wid, plan: dict, version: int):
        now_h = self.now // 60
        self.live[wid] = self._get(f"/workflows/{wid}/config")
        lv = self.live[wid]
        self.store.config_record(wid, lv["version"], lv["actor"], self.now, [], None, None, lv["config"])
        self.store.wf_set(wid, "last_seen_version", lv["version"])
        if plan.get("exp"):
            exp = plan["exp"]
            for dim in pol.DIMS:
                for s in exp["dims"][dim]["scopes"].values():
                    if s["challengers"] and s["started_hour"] is None:
                        s["started_hour"] = now_h
            self.store.wf_set(wid, "exp", exp)
            for d in plan.get("decisions", []):
                self.audit(wid, f"exp_{d['type']}", _decision_text(d), d)
        if plan.get("incident"):
            inc = self.store.wf_get(wid, "incident", {})
            inc.update(status="verifying", verify_from=now_h, fix_prompt=plan["incident"]["prompt"],
                       applied_minute=self.now)
            self.store.wf_set(wid, "incident", inc)
            self.store.wf_set(wid, "prompt_since", {"prompt": plan["incident"]["prompt"], "hour": now_h})
        if plan.get("schedule"):
            self.store.wf_set(wid, "schedule_applied", {"day": self.now // 1440, **plan["schedule"]})

    def reconcile(self):
        """On (re)start: resolve intents written just before a crash, then let poll_governance do the rest."""
        for p in self.store.props(statuses=("intent", "applying")):
            wid = p["workflow_id"]
            if p["path"] == "approval":
                found = [a for a in self._get("/approvals", workflow_id=wid) if a["proposal_id"] == p["proposal_id"]]
                if found:
                    self.store.prop_update(p["proposal_id"], status="pending" if found[0]["status"] == "pending"
                                           else ("approved" if found[0]["status"] in ("approved",) else found[0]["status"]),
                                           approval_id=found[0]["approval_id"])
                    if found[0]["status"] == "applied":
                        self.store.prop_update(p["proposal_id"], status="applying")
                    self.audit(wid, "reconciled", f"{p['proposal_id']} found at simulator as {found[0]['status']}")
                else:
                    self.store.prop_update(p["proposal_id"], status="abandoned")
                    self.audit(wid, "reconciled", f"{p['proposal_id']} never reached the simulator; abandoned")
            else:
                hist = self._get(f"/workflows/{wid}/config/history")
                hit = [h for h in hist if h["proposal_id"] == p["proposal_id"]]
                if hit:
                    self.store.prop_update(p["proposal_id"], status="applied", applied_version=hit[0]["version"])
                    self.live[wid] = self._get(f"/workflows/{wid}/config")
                    self._after_apply(wid, p["plan"] or {}, hit[0]["version"])
                    self.audit(wid, "reconciled", f"auto change {p['proposal_id']} had applied as v{hit[0]['version']}")
                else:
                    self.store.prop_update(p["proposal_id"], status="abandoned")
                    self.audit(wid, "reconciled", f"auto change {p['proposal_id']} never applied; abandoned")
        for p in self.store.props(statuses=("applying",), path="approval"):
            # we crashed between approve and apply-confirm: poll_governance will see applied/approved
            self.store.prop_update(p["proposal_id"], status="approved")

    # ================================================================== inbox
    def process_inbox(self):
        for ev in self.agg.drain_inbox():
            wid = ev.get("workflow_id")
            if ev["type"] == "config_changed":
                seen = self.store.wf_get(wid, "last_seen_version", 0)
                if ev["version"] <= seen:
                    continue
                self.store.wf_set(wid, "last_seen_version", ev["version"])
                self.store.config_record(wid, ev["version"], ev["actor"], self.agg.minute(ev["ts"]),
                                         ev["changed_dims"], ev.get("proposal_id"), ev.get("approval_id"), None)
                if ev["actor"] == "supervisor":
                    self.on_supervisor_edit(wid, ev)
            elif ev["type"] == "campaign_created":
                self.store.wf_set(wid, "batch_day", self.agg.minute(ev["ts"]) // 1440)

    def on_supervisor_edit(self, wid, ev):
        dims = ev["changed_dims"]
        cfg = self._get(f"/workflows/{wid}/config")
        self.live[wid] = cfg
        self.store.config_record(wid, cfg["version"], cfg["actor"], self.now, dims, None, None, cfg["config"])
        touched = set(self.store.wf_get(wid, "supervisor_dims", [])) | set(dims)
        self.store.wf_set(wid, "supervisor_dims", sorted(touched))
        now_h = self.agg.minute(ev["ts"]) // 60
        exp = self.store.wf_get(wid, "exp")
        notes = []
        for dim in pol.DIMS:
            if dim in dims or "cohorts" in dims:
                fresh = pol.new_exp_state(cfg["config"])["dims"][dim]
                fresh["scopes"]["default"]["challengers"] = []
                fresh["scopes"]["default"]["retired"] = exp["dims"][dim]["scopes"]["default"]["retired"]
                fresh["cooldown_until"] = now_h + P.EXP_SUPERVISOR_COOLDOWN_H
                fresh["supervisor_touched"] = True
                exp["dims"][dim] = fresh
                notes.append(f"{dim}: experiment stopped, supervisor's choice "
                             f"{fresh['scopes']['default']['champion']} is the new incumbent; no proposals on this "
                             f"dimension for {P.EXP_SUPERVISOR_COOLDOWN_H}h, and never via auto-apply")
        self.store.wf_set(wid, "exp", exp)
        if "prompt_version" in dims:
            self.store.wf_set(wid, "prompt_since", {"prompt": cfg["config"]["prompt_version"], "hour": now_h})
            notes.append(f"prompt is now {cfg['config']['prompt_version']} (supervisor); watching flag rate")
        self.audit(wid, "supervisor_edit", f"v{ev['version']} by supervisor changed {dims}; respected, not reverted. "
                   + " | ".join(notes), {"event": ev, "config": cfg["config"]})

    # ================================================================== decide
    def decide(self):
        now_h = self.now // 60
        hod = (self.now % 1440) // 60
        self.credits = self.sim.get("credits_remaining", self.credits)
        for wid, wf in self.wfs.items():
            self.live[wid] = self._get(f"/workflows/{wid}/config")
            self.poll_governance(wid)
            self.step_incident(wid, now_h)
            if self.now % 60 == 0:
                self.step_did(wid, now_h)
                if wf["objective"]["primary"] == "engagement_rate":
                    self.step_exp_urgent(wid, now_h, hod)
            self.step_schedule_auto(wid, now_h)
            self.step_routine(wid, now_h, hod)

    # ------------------------------------------------------------------ incidents (C8)
    def step_incident(self, wid, now_h):
        a = self.agg.wf[wid]
        live = self.live[wid]["config"]
        inc = self.store.wf_get(wid, "incident", {"status": "none"})
        base = self.store.wf_get(wid, "hal_base", 0.01)
        cur = live["prompt_version"]
        since = self.store.wf_get(wid, "prompt_since", {"hour": now_h - 720})["hour"]
        if inc["status"] in ("none", "resolved"):
            alarm, ev = pol.hal_check(a, cur, since, now_h, base)
            if alarm:
                inc = {"status": "open", "prompt": cur, "opened_minute": self.now, "evidence": ev, "tried": [],
                       "credits": 0.0, "proposal_id": None, "candidate": None}
                self.store.wf_set(wid, "incident", inc)
                self.audit(wid, "incident_open", f"hallucination flags on {cur}: {ev['flags']} in {ev['connects']} "
                           f"connects ({ev['rate']:.3f} vs base {base:.4f}, p={ev['p_value']:.1e})", ev)
            return
        if inc["status"] == "open":
            if cur != inc["prompt"]:
                inc.update(status="verifying", verify_from=now_h, fix_prompt=cur, applied_minute=self.now)
                self.store.wf_set(wid, "incident", inc)
                self.audit(wid, "incident_update", f"prompt changed to {cur} by someone else; verifying it")
                return
            hours = self.policy_of(wid)["approver_hours"]
            if self.approvals_last_24h(wid) >= self.policy_of(wid)["max_requests_per_24h"]:
                return
            cand = inc.get("candidate") or self.find_fix(wid, inc)
            if not cand:
                return
            inc["candidate"] = cand
            cfg = copy.deepcopy(live)
            cfg["prompt_version"] = cand["prompt"]
            ev = inc["evidence"]
            req = {"change_summary": f"Incident fix: switch prompt {inc['prompt']} -> {cand['prompt']} "
                                     f"(hallucination flags {ev['rate']:.1%} vs normal {base:.1%})",
                   "diff": {"prompt_version": {"old": inc["prompt"], "new": cand["prompt"]}},
                   "affected_leads": int(a.active_by_code().sum()),
                   "dry_run": {"validation": cand["validation"], "candidate_history": cand.get("history"),
                               "method": "synthetic validation run + this workflow's own production history of the prompt"},
                   "impact_score": 0.95,
                   "rationale": f"{inc['prompt']} has been live since hour {since}; flagged-hallucination rate "
                                f"{ev['rate']:.3f} on {ev['connects']} connects vs workflow baseline {base:.4f} "
                                f"(Poisson p={ev['p_value']:.1e}). {cand['why']}",
                   "evidence": {"validation_run_id": cand["validation"]["validation_run_id"], "detector": ev}}
            pid = self.submit_approval(wid, "incident", cfg, req, {"incident": {"prompt": cand["prompt"]}})
            if pid:
                inc.update(status="fix_proposed", proposal_id=pid)
            self.store.wf_set(wid, "incident", inc)
            return
        if inc["status"] == "verifying":
            n, f, e = pol.hal_window(a, inc["verify_from"], now_h, inc["fix_prompt"])
            if n < P.HAL_VERIFY_MIN_CONNECTS:
                return
            from .stats import poisson_sf
            p = poisson_sf(f, base * n)
            if f / n <= 1.5 * base or p >= 0.01:
                inc["status"] = "resolved"
                inc["resolved_minute"] = self.now
                self.audit(wid, "incident_resolved",
                           f"{inc['fix_prompt']} verified: {f} flags in {n} connects ({f / n:.4f} vs base {base:.4f}); "
                           f"time from detection to verified fix {(self.now - inc['opened_minute']) / 60:.1f}h, "
                           f"credits {inc['credits']:.0f}", {"connects": n, "flags": f, "engaged": e})
            else:
                inc["tried"].append(inc["fix_prompt"])
                inc.update(status="open", prompt=inc["fix_prompt"], candidate=None, proposal_id=None)
                self.audit(wid, "incident_reopened", f"{inc['fix_prompt']} still elevated ({f}/{n}); trying next fix")
            self.store.wf_set(wid, "incident", inc)

    def find_fix(self, wid, inc):
        a = self.agg.wf[wid]
        base = self.store.wf_get(wid, "hal_base", 0.01)
        gov = self.policy_of(wid)
        vals = self.store.wf_get(wid, "validations", {})
        tried = set(inc["tried"]) | {inc["prompt"]}

        def validate(prompt):
            if prompt in vals:
                return vals[prompt]
            cost = math.ceil(P.HAL_VALIDATION_N / 100)
            if inc["credits"] + cost > P.HAL_CREDIT_CAP_PER_INCIDENT or self.credits < cost:
                self.audit(wid, "credits", f"not validating {prompt}: incident cap/credits exhausted")
                return None
            code, run = self.c.post("/validation/runs", {"workflow_id": wid, "prompt_version": prompt,
                                                         "n": P.HAL_VALIDATION_N})
            if code != 200:
                self.audit(wid, "validation_failed", f"{prompt}: HTTP {code} {run}")
                return None
            inc["credits"] += cost
            self.credits = run["credits_remaining"]
            vals[prompt] = run
            self.store.wf_set(wid, "validations", vals)
            self.audit(wid, "validation", f"{prompt}: pass {run['pass_rate']:.4f} on n={run['n']} "
                       f"({run['validation_run_id']}, {cost} credits)", run)
            return run

        # 1) last-known-good prompt from our own production history (no LLM credits needed)
        rows = self.store.db.execute("SELECT version, config FROM configs WHERE workflow_id=? AND config IS NOT NULL "
                                     "ORDER BY version DESC", (wid,)).fetchall()
        seen = []
        for _, c in rows:
            p = json.loads(c)["prompt_version"]
            if p not in seen:
                seen.append(p)
        for p in seen:
            if p in tried:
                continue
            n, f, e = pol.hal_window(a, a.base, self.now // 60, p)
            if n >= 300 and f / n <= 1.5 * base:
                run = validate(p)
                if run and pol.validation_ok(run, gov["min_validation_pass_rate"]):
                    return {"prompt": p, "validation": run, "history": {"connects": n, "flags": f, "engaged": e,
                                                                        "engagement": round(e / n, 4)},
                            "why": f"{p} is the last known-good prompt on this workflow ({f}/{n} flags, engagement "
                                   f"{e / n:.3f} in production) and passed validation {run['pass_rate']:.3f} "
                                   f"(our bar: failure rate <= {P.HAL_MAX_FAILURE_RATE})."}
                inc["tried"].append(p)
        # 2) ask the LLM for alternatives (3 credits), validate them, pick the safest that passes our bar
        if self.credits < P.HAL_CREDIT_FLOOR or inc["credits"] + 3 > P.HAL_CREDIT_CAP_PER_INCIDENT:
            return None
        code, sug = self.c.post("/llm/suggest-fixes", {"workflow_id": wid, "problem":
                                f"hallucination flags rose to {inc['evidence']['rate']} on prompt {inc['prompt']}"})
        if code != 200:
            return None
        inc["credits"] += 3
        self.audit(wid, "llm_suggest", f"suggestions: {[s['prompt_version'] for s in sug['suggestions']]}", sug)
        passing = []
        for s in sug["suggestions"]:
            if s["prompt_version"] in tried:
                continue
            run = validate(s["prompt_version"])
            if run and pol.validation_ok(run, gov["min_validation_pass_rate"]):
                passing.append((run["failures"], s["prompt_version"], run, s["summary"]))
        if not passing:
            self.audit(wid, "incident_no_fix", "no candidate passed validation; paging on-call")
            return None
        passing.sort()
        _, p, run, summ = passing[0]
        return {"prompt": p, "validation": run, "why": f"LLM-suggested {p} ('{summ}') passed validation "
                f"{run['pass_rate']:.3f}; engagement impact unknown and will be checked after apply."}

    # ------------------------------------------------------------------ caller ID (C5)
    def step_did(self, wid, now_h):
        a = self.agg.wf[wid]
        st, notes = pol.did_assess(a, now_h, self.store.wf_get(wid, "did", {}))
        self.store.wf_set(wid, "did", st)
        for n in notes:
            self.audit(wid, "did_status", n, {d: s for d, s in st.items()})
        live = self.live[wid]["config"]
        cur = live["allocations"]["default"]["caller_id"]
        auto = self.auto_allowed(wid, ["caller_id"])
        target = pol.did_target(a, st, cur, auto)
        if tv_distance(cur, target) < 0.02:
            return
        urgent = any(st[d]["status"] == "degraded" and cur.get(d, 0) >= 0.05 for d in st)
        if auto:
            if self.approval_in_flight(wid) and not urgent:
                return
            step = pol.step_towards(cur, target, self.policy_of(wid)["auto_apply"]["max_shift"])
            cfg = copy.deepcopy(live)
            cfg["allocations"]["default"]["caller_id"] = step
            exp_old, exp_new = pol.did_expected_pickup(st, cur), pol.did_expected_pickup(st, step)
            states = ", ".join(f"{d}={s['status']}" for d, s in st.items())
            why = (f"caller IDs {states}; "
                   f"{'quarantine' if urgent else 'rebalance'} {cur} -> {step} (target {target}); "
                   f"spreading volume across healthy numbers limits spam-flag load")
            self.submit_auto(wid, "quarantine" if urgent else "did", cfg, why,
                             {"did_status": st, "expected_pickup_ratio_old": round(exp_old, 3),
                              "expected_pickup_ratio_new": round(exp_new, 3)}, {"decisions": []})
        elif urgent and not self.approval_in_flight(wid) and \
                self.approvals_last_24h(wid) < self.policy_of(wid)["max_requests_per_24h"] and self._approver_open(wid):
            cfg = copy.deepcopy(live)
            cfg["allocations"]["default"]["caller_id"] = target
            self._propose(wid, "quarantine", cfg, [], f"Quarantine degraded caller ID(s): {cur} -> {target}")

    def _approver_open(self, wid, routine=False) -> bool:
        lo, hi = self.policy_of(wid)["approver_hours"]
        if routine:
            lo = max(lo, P.TENANT_PREFS.get(self.wfs[wid]["tenant_id"], {}).get("earliest_request_hour", lo))
            hi = hi - P.LATEST_ROUTINE_BEFORE_END_H
        hod = (self.now % 1440) / 60
        return lo <= hod < hi

    # ------------------------------------------------------------------ call time (C4)
    def step_schedule_auto(self, wid, now_h):
        if not self.auto_allowed(wid, ["schedule"]):
            return
        day = self.now // 1440
        if self.store.wf_get(wid, "batch_day") != day or self.store.wf_get(wid, "sched_day") == day:
            return
        if self.approval_in_flight(wid):
            return
        live = self.live[wid]["config"]
        cfg, info = self._schedule_cfg(wid, live)
        self.store.wf_set(wid, "sched_day", day)
        if cfg is None:
            return
        self.submit_auto(wid, "schedule", cfg,
                         f"daily call-time refresh: {info['mapped']} people in live campaigns get a preferred hour from "
                         f"their (age band, city tier) pickup profile; expected pickup lift "
                         f"{info['dry_run']['pickup_lift_expected']:+.1%}",
                         {"dry_run": info["dry_run"], "peaks": info["peaks"]}, {"schedule": {"mapped": info["mapped"]}})

    def _schedule_cfg(self, wid, live):
        a = self.agg.wf[wid]
        prof = pol.hour_profiles(self.agg, a, self.wfs[wid]["dialling_window"])
        dry = pol.schedule_dry_run(prof)
        if dry["pickup_lift_upper_bound"] < 0.01:
            return None, None
        mp, peaks = pol.preferred_hours(self.agg, a, prof)
        if len(mp) > 200_000:
            mp = dict(list(mp.items())[:200_000])
        cfg = copy.deepcopy(live)
        cfg["schedule"]["lead_preferred_hour"] = mp
        if _norm_cfg(cfg) == _norm_cfg(live):
            return None, None
        return cfg, {"mapped": len(mp), "dry_run": dry, "peaks": peaks}

    # ------------------------------------------------------------------ experiments (C6/C7)
    def step_exp_urgent(self, wid, now_h, hod):
        exp = self.store.wf_get(wid, "exp")
        live = self.live[wid]["config"]
        new, decisions, urgent = pol.plan_experiments(self.agg, self.agg.wf[wid], exp, live, now_h, urgent_only=True)
        if not urgent or self.approval_in_flight(wid):
            return
        cfg = pol.compile_config(live, new)
        if _norm_cfg(cfg) == _norm_cfg(live):
            self.store.wf_set(wid, "exp", new)
            return
        dims = [d for d in pol.DIMS if cfg["allocations"] != live["allocations"]]
        from .stats import tv_distance as tv
        shift_ok = all(tv(_dist(cfg, c, d), _dist(live, c, d)) <= self.policy_of(wid)["auto_apply"]["max_shift"] + 1e-9
                       for c in {"default"} | set(cfg["allocations"]) for d in pol.DIMS)
        if self.auto_allowed(wid, list(pol.DIMS)) and cfg.get("cohorts") == live.get("cohorts") and shift_ok:
            self.submit_auto(wid, "rollback", cfg, "; ".join(_decision_text(d) for d in decisions),
                             {"decisions": decisions}, {"exp": new, "decisions": decisions})
            return
        if self.approvals_last_24h(wid) < self.policy_of(wid)["max_requests_per_24h"] and self._approver_open(wid):
            self._propose(wid, "rollback", cfg, decisions, None, exp=new)

    def step_routine(self, wid, now_h, hod):
        day = self.now // 1440
        if self.now < 0 or not self._approver_open(wid, routine=True):
            return
        done = self.store.wf_get(wid, "routine_day")
        retry = self.store.wf_get(wid, "routine_retry_day")
        if done == day and retry != day:
            return
        if self.approval_in_flight(wid) or self.approvals_last_24h(wid) >= self.routine_cap(wid):
            return
        wf = self.wfs[wid]
        live = self.live[wid]["config"]
        cfg = copy.deepcopy(live)
        plan = {"decisions": []}
        touched = set(self.store.wf_get(wid, "supervisor_dims", []))
        if wf["objective"]["primary"] == "engagement_rate":
            exp = self.store.wf_get(wid, "exp")
            new, decisions, _ = pol.plan_experiments(self.agg, self.agg.wf[wid], exp, live, now_h)
            cfg = pol.compile_config(cfg, new)
            plan.update(exp=new, decisions=decisions)
        if not self.auto_allowed(wid, ["caller_id"]):
            st = self.store.wf_get(wid, "did", {})
            cur = live["allocations"]["default"]["caller_id"]
            tgt = pol.did_target(self.agg.wf[wid], st, cur, auto=False)
            if tv_distance(cur, tgt) >= P.DID_MIN_TV_TO_PROPOSE:
                cfg["allocations"]["default"]["caller_id"] = tgt
                plan["decisions"].append({"type": "did_rebalance", "from": cur, "to": tgt,
                                          "status": {d: s["status"] for d, s in st.items()}})
        if not self.auto_allowed(wid, ["schedule"]) and "schedule" not in touched:
            scfg, info = self._schedule_cfg(wid, cfg)
            if scfg is not None:
                cfg = scfg
                plan["schedule"] = {"mapped": info["mapped"]}
                plan["decisions"].append({"type": "schedule", "mapped": info["mapped"], "dry_run": info["dry_run"]})
        self.store.wf_set(wid, "routine_day", day)
        self.store.wf_set(wid, "routine_retry_day", None)
        if _norm_cfg(cfg) == _norm_cfg(live):
            if plan.get("exp"):
                self.store.wf_set(wid, "exp", plan["exp"])
            return
        self._propose(wid, "routine", cfg, plan["decisions"], None, exp=plan.get("exp"), plan=plan)

    def _propose(self, wid, kind, cfg, decisions, summary, exp=None, plan=None):
        a = self.agg.wf[wid]
        wf = self.wfs[wid]
        live = self.live[wid]["config"]
        now_h = self.now // 60
        plan = plan or {"decisions": decisions}
        if exp is not None:
            plan["exp"] = exp
        dry = {}
        if cfg["allocations"] != live["allocations"] or cfg.get("cohorts") != live.get("cohorts"):
            dry["engagement"] = pol.exp_dry_run(self.agg, a, live, cfg, now_h)
        st = self.store.wf_get(wid, "did", {})
        oc, nc = live["allocations"]["default"]["caller_id"], cfg["allocations"]["default"]["caller_id"]
        if oc != nc:
            dry["caller_id"] = {"expected_pickup_ratio_old": round(pol.did_expected_pickup(st, oc), 3),
                                "expected_pickup_ratio_new": round(pol.did_expected_pickup(st, nc), 3),
                                "status": {d: s["status"] for d, s in st.items()}}
        for d in decisions:
            if d["type"] == "schedule":
                dry["schedule"] = d["dry_run"]
        if not dry:
            dry = {"note": "no measurable effect predicted"}
        primary = wf["objective"]["primary"]
        if primary == "engagement_rate":
            lift = dry.get("engagement", {}).get("combined_engagement_lift", 0.0)
        else:
            lift = (dry.get("caller_id", {}).get("expected_pickup_ratio_new", 1) /
                    max(dry.get("caller_id", {}).get("expected_pickup_ratio_old", 1), 1e-6) - 1) + \
                dry.get("schedule", {}).get("pickup_lift_expected", 0)
        types = {d["type"] for d in decisions}
        if kind in ("rollback", "quarantine"):
            impact = min(1.0, 0.5 + max(lift, 0) / 0.1)
        elif types <= {"start"} and types:
            impact = 0.15  # exploration: information value, no lift claimed
        else:
            impact = max(0.05, min(1.0, lift / 0.10))
        if not summary:
            parts = [_decision_text(d, short=True) for d in decisions]
            summary = f"{kind}: " + ("; ".join(parts) if parts else "config refresh")
        req = {"change_summary": summary[:300],
               "diff": _diff(live, cfg),
               "affected_leads": pol.affected_leads(self.agg, a, live, cfg, wf["mid_flight"],
                                                    a.last_batch_size or wf["daily_batch"]["size"]),
               "dry_run": dry,
               "impact_score": round(float(impact), 3),
               "rationale": " | ".join(_decision_text(d) for d in decisions) or summary,
               "evidence": {"decisions": decisions}}
        return self.submit_approval(wid, kind, cfg, req, plan)

    # ================================================================== main loop
    def run(self):
        t0 = time.time()
        self.catch_up()
        self.process_inbox()
        self.reconcile()
        self.bootstrap()
        self.checkpoint()
        while True:
            self.sim = self._get("/sim/state")
            if self.stop_after is not None and self.now >= self.stop_after:
                log.warning("stop-after reached at %s (simulated crash)", self.ts())
                os._exit(3)
            td = time.time()
            if self.now >= 0:
                self.decide()
            decide_s = time.time() - td
            self.store.meta_set("last_sim_minute", self.now)
            if self.now % 60 == 0:
                from .view import write_view
                write_view(self.store, self.now)
            if self.sim["finished"]:
                break
            ta = time.time()
            code, r = self.c.post("/sim/advance", {"minutes": P.TICK_MINUTES})
            if code == 409:
                break
            if code != 200:
                raise RuntimeError(f"advance failed {code} {r}")
            adv_s = time.time() - ta
            ti = time.time()
            n = self.catch_up()
            self.sim = self._get("/sim/state")
            self.process_inbox()
            ing_s = time.time() - ti
            self.tick_n += 1
            self.store.db.execute("INSERT OR REPLACE INTO loop_metrics VALUES (?,?,?,?,?,?,?,?)",
                                  (self.now, time.time(), adv_s, ing_s, decide_s, n,
                                   self.sim["credits_remaining"], _rss_mb()))
            if self.tick_n % P.CHECKPOINT_EVERY_TICKS == 0:
                self.write_kpis()
                self.checkpoint()
            if self.now % 1440 == 0:
                log.info("day %d done, wall %.0fs, events %d", self.now // 1440, time.time() - t0, self.agg.events)
        self.write_kpis()
        self.checkpoint()
        from .view import write_view
        write_view(self.store, self.now)
        self.audit(None, "finished", f"scenario finished at {self.ts()}; wall {time.time() - t0:.0f}s")

    def write_kpis(self):
        for wid, a in self.agg.wf.items():
            lo = max(a.hidx(-24 * 60), 0)
            hi = a.hidx(self.now // 60)
            for h in range(max(lo, hi - 8), hi):
                d = a.did[h].sum(axis=0)
                hal = a.hal[h].sum(axis=0)
                if d[0] == 0:
                    continue
                self.store.db.execute("INSERT OR REPLACE INTO kpi_hourly VALUES (?,?,?,?,?,?)",
                                      (wid, h + a.base, int(d[0]), int(d[1]), int(hal[2]), int(hal[1])))


def _dist(cfg, cohort, dim):
    return (cfg["allocations"].get(cohort) or {}).get(dim) or cfg["allocations"]["default"][dim]


def _diff(old: dict, new: dict) -> dict:
    out = {}
    keys = set(old["allocations"]) | set(new["allocations"])
    for k in sorted(keys):
        for d in ("welcome_message", "voice", "caller_id"):
            a, b = (old["allocations"].get(k) or {}).get(d), (new["allocations"].get(k) or {}).get(d)
            if a != b:
                out[f"allocations.{k}.{d}"] = {"old": a, "new": b}
    if old.get("cohorts") != new.get("cohorts"):
        out["cohorts"] = {"old": old.get("cohorts"), "new": new.get("cohorts")}
    if old.get("prompt_version") != new.get("prompt_version"):
        out["prompt_version"] = {"old": old["prompt_version"], "new": new["prompt_version"]}
    so, sn = old.get("schedule", {}), new.get("schedule", {})
    if so != sn:
        out["schedule"] = {"lead_preferred_hour_entries": {"old": len(so.get("lead_preferred_hour", {})),
                                                           "new": len(sn.get("lead_preferred_hour", {}))},
                           "hour_weights_changed": so.get("hour_weights") != sn.get("hour_weights")}
    return out or {"note": "no effective change"}


def _decision_text(d: dict, short=False) -> str:
    t = d["type"]
    dim = d.get("dim", "")
    if t == "start":
        return f"test {dim} {d['challengers']} against {d['champion']} ({d['alloc']})" if not short else \
            f"test {dim} {'/'.join(d['challengers'])} vs {d['champion']}"
    if t in ("promote", "segment_winner"):
        s = f"promote {dim} {d['option']} over {d['vs']} in {d['scope']}: lift {d['lift']:+.1%}, z={d['z']:.2f}"
        return s if short else s + f" (crit {d.get('z_crit')}, n={int(d['n'])}/{int(d['n_vs'])}, " \
                                   f"rates {d['rate']:.3f} vs {d['rate_vs']:.3f}); old champion kept as 20% holdout"
    if t == "protect_segment":
        return f"keep {d['vs']} for {d['scope']} ({d['option']} {d['lift']:+.1%} there, z={d['z']:.2f})"
    if t in ("retire_loser", "retire_futile"):
        return f"stop {dim} {d['option']} ({t[7:]}: lift {d['lift']:+.1%} vs {d['vs']}, z={d['z']:.2f}, n={int(d['n'])})"
    if t == "rollback":
        return (f"roll back {dim} {d['from']} -> {d['to']} in {d['scope']}: lift now {d['window']['lift']:+.1%} "
                f"vs {d['promoted_on']['lift']:+.1%} at promotion (z_drop={d['z_drop']})")
    if t == "did_rebalance":
        return f"caller IDs {d['from']} -> {d['to']} ({d['status']})"
    if t == "schedule":
        return f"call-time: {d['mapped']} people get a preferred hour (expected pickup " \
               f"{d['dry_run']['pickup_lift_expected']:+.1%})"
    return json.dumps(d)[:200]
