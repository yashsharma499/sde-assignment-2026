"""Supervisor view (C11), audit queries (C12) and loop health (C14) — all read from the controller's own
SQLite store, never from the simulator's logs.

  python -m controller.view --state state/run                      # one screen per workflow
  python -m controller.view --state state/run why ks_cancel "D3 14:00"   # why did X change around T?
  python -m controller.view --state state/run health               # ingest lag, decisions/hour, credits
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def fmt_min(m):
    if m is None:
        return "-"
    d, r = divmod(int(m), 1440)
    return f"D{d} {r // 60:02d}:{r % 60:02d}"


def parse_when(s: str) -> int:
    day, hm = s.strip().split()
    h, m = hm.split(":")
    return int(day.lstrip("Dd")) * 1440 + int(h) * 60 + int(m)


def _wf_state(db, wid, key, default=None):
    r = db.execute("SELECT v FROM wf_state WHERE workflow_id=? AND key=?", (wid, key)).fetchone()
    return json.loads(r[0]) if r else default


def supervisor_view(db: sqlite3.Connection, now: int) -> str:
    L = [f"# Supervisor view - {fmt_min(now)}", ""]
    wids = [r[0] for r in db.execute("SELECT DISTINCT workflow_id FROM wf_state ORDER BY workflow_id")]
    for wid in wids:
        L.append(f"## {wid}")
        exp = _wf_state(db, wid, "exp") or {"dims": {}}
        L.append("**Being tested / winning**")
        for dim, ed in exp["dims"].items():
            for sid, s in ed["scopes"].items():
                bits = [f"champion **{s['champion']}**"]
                if s.get("control"):
                    bits.append(f"holdout {s['control']}")
                if s["challengers"]:
                    bits.append(f"testing {', '.join(s['challengers'])} since {fmt_min((s['started_hour'] or 0) * 60) if s['started_hour'] is not None else 'pending approval'}")
                if s.get("promo"):
                    bits.append(f"promoted on {s['promo']['lift']:+.1%}")
                if s["retired"]:
                    bits.append("stopped: " + "; ".join(f"{k} ({v})" for k, v in s["retired"].items()))
                cd = ed.get("cooldown_until")
                if cd and cd * 60 > now:
                    bits.append(f"paused after supervisor edit until {fmt_min(cd * 60)}")
                L.append(f"- {dim} [{sid}]: " + ", ".join(bits))
        did = _wf_state(db, wid, "did") or {}
        L.append("**Caller IDs**: " + ", ".join(f"{d} {s['status']} ({s.get('ratio_24h', '-')}x)" for d, s in did.items()))
        inc = _wf_state(db, wid, "incident") or {"status": "none"}
        if inc["status"] not in ("none",):
            L.append(f"**Incident**: {inc['status']} - prompt {inc.get('prompt')} opened {fmt_min(inc.get('opened_minute'))}"
                     + (f", fix {inc.get('fix_prompt')}" if inc.get("fix_prompt") else ""))
        pend = db.execute("SELECT kind, approval_id, request, created_minute FROM proposals WHERE workflow_id=? AND "
                          "path='approval' AND status IN ('pending','approved','applying')", (wid,)).fetchall()
        L.append("**Waiting for you**: " + ("; ".join(
            f"{a} ({k}) {json.loads(r)['change_summary']} - impact {json.loads(r)['impact_score']}" for k, a, r, _ in pend)
            if pend else "nothing"))
        day0 = (now // 1440) * 1440
        ch = db.execute("SELECT ts, kind, summary FROM audit WHERE workflow_id=? AND sim_minute>=? AND kind IN "
                        "('applied','auto_applied','supervisor_edit','incident_open','incident_resolved','approval_rejected',"
                        "'approval_expired','apply_stale') ORDER BY id", (wid, day0)).fetchall()
        L.append("**Changed today**:" + ("" if ch else " nothing"))
        for ts, k, s in ch:
            L.append(f"- {ts[11:16]} {k}: {s[:220]}")
        L.append("")
    return "\n".join(L)


def write_view(store, now):
    try:
        (store.dir / "supervisor_view.md").write_text(supervisor_view(store.db, now), encoding="utf-8")
    except Exception as e:  # the view must never take the loop down
        print("view error", e)


def why(db, wid: str, when: str, hours: int = 6) -> str:
    t = parse_when(when)
    L = [f"# Why did {wid} change around {when}?", ""]
    rows = db.execute("SELECT proposal_id, path, kind, status, approval_id, created_minute, applied_minute, "
                      "applied_version, request, plan FROM proposals WHERE workflow_id=? AND applied_minute "
                      "BETWEEN ? AND ? ORDER BY applied_minute", (wid, t - hours * 60, t + hours * 60)).fetchall()
    sup = db.execute("SELECT ts, summary FROM audit WHERE workflow_id=? AND kind='supervisor_edit' AND sim_minute "
                     "BETWEEN ? AND ?", (wid, t - hours * 60, t + hours * 60)).fetchall()
    for ts, s in sup:
        L.append(f"- {ts[5:16]} SUPERVISOR: {s}")
    if not rows and not sup:
        L.append(f"No controller or supervisor change within {hours}h of {when}.")
    for pid, path, kind, status, aid, cm, am, av, req, plan in rows:
        req = json.loads(req) if req else {}
        L += [f"## v{av} applied {fmt_min(am)} via {'approval ' + aid if path == 'approval' else 'policy auto-apply'}"
              f" ({kind})", f"- proposal: `{pid}` created {fmt_min(cm)}"]
        if path == "approval":
            dec = db.execute("SELECT ts, summary FROM audit WHERE proposal_id=? AND kind='applied'", (pid,)).fetchone()
            L.append(f"- approved by the {wid} approver ({aid}); {dec[1] if dec else ''}")
        L.append(f"- summary: {req.get('change_summary') or req.get('rationale')}")
        L.append(f"- rationale: {req.get('rationale')}")
        if req.get("impact_score") is not None:
            L.append(f"- impact score {req['impact_score']}, affected leads {req.get('affected_leads')}")
            L.append(f"- dry run: `{json.dumps(req.get('dry_run'))[:600]}`")
        # what happened next: KPIs 24h before vs after, and follow-up audit events
        h = am // 60
        def kpi(a, b):
            r = db.execute("SELECT SUM(dials), SUM(connected), SUM(engaged), SUM(flags) FROM kpi_hourly WHERE "
                           "workflow_id=? AND hour>=? AND hour<?", (wid, a, b)).fetchone()
            d, c, e, f = (x or 0 for x in r)
            return f"pickup {c / d:.3f}, engagement {e / c:.3f}, flags {f / c:.4f} ({d} dials)" if d and c else "no data"
        L.append(f"- before (24h): {kpi(h - 24, h)}")
        L.append(f"- after  (24h): {kpi(h, h + 24)}")
        nxt = db.execute("SELECT ts, kind, summary FROM audit WHERE workflow_id=? AND sim_minute>? AND sim_minute<=? "
                         "AND kind NOT IN ('did_status') ORDER BY id LIMIT 8", (wid, am, am + 1440)).fetchall()
        L.append("- next events:")
        L += [f"  - {ts[5:16]} {k}: {s[:200]}" for ts, k, s in nxt]
    return "\n".join(L)


def health(db) -> str:
    r = db.execute("SELECT COUNT(*), SUM(events), SUM(ingest_s), SUM(advance_s), SUM(decide_s), MAX(rss_mb), "
                   "MIN(credits), MIN(sim_minute), MAX(sim_minute) FROM loop_metrics").fetchone()
    n, ev, ing, adv, dec, rss, cred, lo, hi = r
    if not n:
        return "no metrics yet"
    days = max((hi - lo) / 1440, 1e-9)
    decisions = db.execute("SELECT COUNT(*) FROM audit WHERE kind IN ('auto_applied','proposal_submitted','applied')"
                           ).fetchone()[0]
    return "\n".join([
        f"ticks {n}, simulated days {days:.2f}",
        f"events ingested {ev:,} -> {ev / max(ing, 1e-9):,.0f} events/s (ingest wall {ing:.0f}s)",
        f"simulator advance wall {adv:.0f}s, decide wall {dec:.0f}s, controller wall per sim-day "
        f"{(ing + dec) / days:.1f}s (+ simulator {adv / days:.1f}s)",
        f"peak RSS {rss:.0f} MB, credits remaining {cred}",
        f"decisions (proposals/applies) {decisions} -> {decisions / (days * 24):.2f}/hour",
    ])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("cmd", nargs="?", default="view")
    ap.add_argument("args", nargs="*")
    a = ap.parse_args()
    db = sqlite3.connect(Path(a.state) / "controller.db")
    if a.cmd == "why":
        print(why(db, a.args[0], a.args[1]))
    elif a.cmd == "health":
        print(health(db))
    elif a.cmd == "audit":
        q = "SELECT ts, workflow_id, kind, summary FROM audit"
        args = []
        if a.args:
            q += " WHERE workflow_id=?"
            args.append(a.args[0])
        for ts, w, k, s in db.execute(q + " ORDER BY id", args):
            print(f"{ts[5:16]} {w or '-':12} {k:20} {s}")
    else:
        now = db.execute("SELECT MAX(sim_minute) FROM loop_metrics").fetchone()[0] or 0
        print(supervisor_view(db, now))


if __name__ == "__main__":
    main()
