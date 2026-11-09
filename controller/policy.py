"""Decision logic. Pure functions of (aggregates, committed state, live config) -> proposed state + evidence.

Nothing here talks HTTP or writes the store; `loop.py` does that, so every decision is reproducible from
the same inputs (and unit-testable).
"""
from __future__ import annotations

import copy
import math
import zlib

import numpy as np

from . import params as P
from .aggregates import Aggregates, WfAgg
from .stats import (mh_risk_difference, observed_vs_expected, poisson_sf, quantize, step_towards, tv_distance,
                    wilson_upper, z_for_alpha)

DIMS = ("welcome_message", "voice")
OUR_COHORT_PREFIX = "fx:"


def top_option(dist: dict) -> str:
    return max(sorted(dist), key=lambda k: dist[k])


# =============================================================================== caller ID health (C5)
def did_window(a: WfAgg, h_from: int, h_to: int):
    lo, hi = max(a.hidx(h_from), 0), max(a.hidx(h_to), 0)
    return a.did[lo:hi]  # (T, nD, 2)


def did_assess(a: WfAgg, now_h: int, state: dict) -> tuple[dict, list[str]]:
    """Update per-DID status from data. Returns (new_state, notes)."""
    st = copy.deepcopy(state)
    notes = []
    for d in a.D:
        st.setdefault(d, {"status": "unknown", "since": now_h})

    def compare(i, window_h):
        # pooled against every other number that dialled in the window: comparing only against a fresh,
        # unloaded number would make every loaded number look "degraded" (public_01 run 2 did exactly that)
        win = did_window(a, now_h - window_h, now_h)
        others = [j for j in range(len(a.D)) if j != i]
        dd, dc = win[:, i, 0], win[:, i, 1]
        od, oc = win[:, others, 0].sum(axis=1), win[:, others, 1].sum(axis=1)
        return observed_vs_expected(dc, dd, oc, od)

    for i, d in enumerate(a.D):
        s = st[d]
        old = s["status"]
        if old == "degraded":
            wait = P.DID_REPROBE_AFTER_H * 2 ** s.get("quarantines", 0)
            if now_h - s["since"] >= wait:
                s.update(status="unknown", since=now_h, reprobe=True)
                notes.append(f"{d}: quarantined {wait}h, re-probing at {P.DID_REPROBE_SHARE:.0%}")
            continue
        r48, z48, O48, E48, n48 = compare(i, 48)
        own = did_window(a, now_h - 48, now_h)[:, i, 0].sum()
        if old == "unknown" and E48 == 0 and own >= 10 * P.DID_MIN_DIALS_JUDGE:
            # the only number in use: nothing to compare against, so it is the incumbent baseline
            s.update(status="healthy", since=now_h, note="sole number in use; baseline by definition")
        elif old == "unknown":
            if n48 >= P.DID_MIN_DIALS_JUDGE and O48 > 0 and r48 * math.exp(-1.645 / math.sqrt(O48)) >= P.DID_HEALTHY_LB:
                s.update(status="healthy", since=now_h)
            elif n48 >= 100 and r48 < P.DID_DEGRADED_RATIO and z48 <= P.DID_DEGRADED_Z:
                s.update(status="degraded", since=now_h)
        elif old == "healthy":
            rf, zf, *_ = compare(i, P.DID_FAST_WINDOW_H)
            rs, zs, *_ = compare(i, P.DID_SLOW_WINDOW_H)
            bad = (rf < P.DID_FAST_RATIO and zf <= P.DID_FAST_Z) or (rs < P.DID_DEGRADED_RATIO and zs <= P.DID_DEGRADED_Z)
            n_healthy_other = sum(1 for x in a.D if x != d and st[x]["status"] == "healthy")
            if bad and (n_healthy_other >= P.DID_MIN_HEALTHY or min(rf, rs) < P.DID_CATASTROPHIC_RATIO):
                s.update(status="degraded", since=now_h)
            elif bad:
                notes.append(f"{d}: weak ({rs:.2f}x) but kept - quarantining would leave <{P.DID_MIN_HEALTHY} numbers")
        if s["status"] == "degraded" and old != "degraded":
            s["quarantines"] = s.get("quarantines", 0) + 1
            s.pop("reprobe", None)
        if s["status"] == "healthy":
            s.pop("reprobe", None)
        r24, z24, O24, E24, n24 = compare(i, 24)
        s.update(ratio_24h=round(r24, 3), z_24h=round(z24, 2), dials_24h=int(n24))
        if s["status"] != old:
            notes.append(f"{d}: {old} -> {s['status']} (pickup vs other numbers {r24:.2f}x over 24h, z={z24:.1f}, "
                         f"48h {r48:.2f}x on {int(n48)} dials)")
    return st, notes


def did_target(a: WfAgg, st: dict, current: dict, auto: bool) -> dict:
    healthy = [d for d in a.D if st[d]["status"] == "healthy"]
    unknown = [d for d in a.D if st[d]["status"] == "unknown"]
    if not healthy:
        return dict(current)
    probe = P.DID_PROBE_SHARE_AUTO if auto else P.DID_PROBE_SHARE_APPROVAL
    probe = min(probe, 0.3 / max(len(unknown), 1)) if unknown else 0.0
    shares = {d: (P.DID_REPROBE_SHARE if st[d].get("reprobe") else probe) for d in unknown}
    rest = 1.0 - sum(shares.values())
    out = {d: rest / len(healthy) for d in healthy}
    out.update(shares)
    return quantize(out)


def did_expected_pickup(st: dict, dist: dict) -> float:
    return sum(w * st.get(d, {}).get("ratio_24h", 1.0) for d, w in dist.items()) / (sum(dist.values()) or 1)


# =============================================================================== call time (C4)
def hour_profiles(agg: Aggregates, a: WfAgg, window: list[int]) -> dict:
    """Per (age_band, city_tier) cell: pickup index by hour relative to the cell's own average, attempt-
    bucket standardised, shrunk to the age-band profile. Learned from history + live data."""
    lo, hi = window
    H = a.pick.shape[0]
    hod = (np.arange(H) + a.base) % 24
    rows = agg.codes.rows
    n = len(rows)
    pick = a.pick[:, :n]  # (H, C, 3, 2)
    by_hour = np.zeros((24, n, 3, 2))
    np.add.at(by_hour, hod, pick)
    hours = list(range(lo, hi))

    def index_for(codes, prior):
        sub = by_hour[:, codes].sum(axis=1)  # (24, 3, 2)
        tot = sub[hours].sum(axis=0)
        p_att = np.where(tot[:, 0] > 0, tot[:, 1] / np.maximum(tot[:, 0], 1), 0)
        out = {}
        for h in hours:
            E = float((sub[h, :, 0] * p_att).sum())
            O = float(sub[h, :, 1].sum())
            pr = prior.get(h, 1.0) if prior else 1.0
            out[h] = (O + P.SCHED_PRIOR_STRENGTH * pr) / (E + P.SCHED_PRIOR_STRENGTH)
        return out, float(tot[:, 0].sum())

    prof = {}
    for age in sorted({r["age_band"] for r in rows}):
        ac = [i for i, r in enumerate(rows) if r["age_band"] == age]
        aidx, _ = index_for(ac, None)
        for tier in sorted({r["city_tier"] for r in rows}):
            cc = [i for i in ac if rows[i]["city_tier"] == tier]
            if not cc:
                continue
            cidx, dials = index_for(cc, aidx)
            prof[(age, tier)] = {"index": cidx, "dials": dials}
    return prof


def preferred_hours(agg: Aggregates, a: WfAgg, prof: dict) -> tuple[dict, dict]:
    """person_id -> preferred hour for persons in live campaigns whose cell has a clear peak."""
    cand = {}
    for cell, p in prof.items():
        idx = p["index"]
        best = max(idx.values())
        if best < P.SCHED_MIN_PEAK_INDEX:
            continue
        hs = sorted(h for h, v in idx.items() if v >= best - P.SCHED_TOP_BAND)
        cand[cell] = (hs, [idx[h] for h in hs])
    out = {}
    rows = agg.codes.rows
    for pid, (code, _) in a.person_code.items():
        r = rows[code]
        c = cand.get((r["age_band"], r["city_tier"]))
        if not c:
            continue
        hs, ws = c
        u = (zlib.crc32(pid.encode()) % 10_000) / 10_000 * sum(ws)
        acc = 0.0
        for h, w in zip(hs, ws):
            acc += w
            if u <= acc:
                out[pid] = int(h)
                break
        else:
            out[pid] = int(hs[-1])
    summary = {f"{k[0]}|tier{k[1]}": v[0] for k, v in cand.items()}
    return out, summary


def schedule_dry_run(prof: dict) -> dict:
    """Replay against history: mean pickup index of the hours we'd steer each cell to (dial-weighted)."""
    num = den = 0.0
    for cell, p in prof.items():
        idx = p["index"]
        best = max(idx.values())
        if best < P.SCHED_MIN_PEAK_INDEX:
            continue
        hs = [h for h, v in idx.items() if v >= best - P.SCHED_TOP_BAND]
        num += p["dials"] * (np.mean([idx[h] for h in hs]) - 1.0)
        den += p["dials"]
    tot = sum(p["dials"] for p in prof.values()) or 1
    upper = num / tot
    return {"method": "history replay of pickup-by-hour per (age_band, city_tier), attempt-standardised",
            "cells_with_clear_peak_share": round(den / tot, 3),
            "pickup_lift_upper_bound": round(upper, 4),
            "pickup_lift_expected": round(upper * 0.5, 4),
            "note": "upper bound assumes every attempt lands in a preferred hour; capacity and cadence gaps "
                    "make ~half realistic. Verified after apply by the pickup drift monitor."}


# =============================================================================== experiments (C6, C7)
def new_exp_state(cfg: dict) -> dict:
    st = {"dims": {}}
    for dim in DIMS:
        dist = cfg["allocations"]["default"][dim]
        champ = top_option(dist)
        st["dims"][dim] = {"scopes": {"default": _scope(champ, [o for o in dist if o != champ and dist[o] > 0])},
                           "cooldown_until": None, "supervisor_touched": False}
    return st


def _scope(champ, challengers=(), where=None, control=None):
    return {"champion": champ, "control": control, "challengers": list(challengers), "retired": {},
            "started_hour": None, "promo": None, "where": where, "priority": 0.0}


def scope_alloc(s: dict) -> dict:
    ch = s["challengers"]
    ctrl = s["control"] if s["control"] and s["control"] != s["champion"] else None
    ctrl_share = P.EXP_CONTROL_SHARE if ctrl else 0.0
    ch_total = (1 - P.EXP_CHAMPION_SHARE - ctrl_share) if ch else 0.0
    if ch and ch_total < 0.2:
        ch_total = 0.2
    out = {s["champion"]: 1.0 - ctrl_share - ch_total}
    if ctrl:
        out[ctrl] = ctrl_share
    for o in ch:
        out[o] = out.get(o, 0) + ch_total / len(ch)
    return quantize({k: v for k, v in out.items() if v > 0})


def foreign_cohorts(cfg: dict) -> list[dict]:
    return [c for c in cfg.get("cohorts", []) if not c["id"].startswith(OUR_COHORT_PREFIX)]


def compile_config(live: dict, exp: dict) -> dict:
    """Experiment state -> config cohorts/allocations. Exceptions are single-feature segments; leads in
    two exceptions (one per dim) get a pair cohort first so neither dimension falls back to default."""
    cfg = copy.deepcopy(live)
    foreign = foreign_cohorts(live)
    for dim in DIMS:
        cfg["allocations"]["default"][dim] = scope_alloc(exp["dims"][dim]["scopes"]["default"])
    if foreign:  # someone else owns cohorts here: we only touch default allocations
        return cfg
    exc = {dim: sorted([(sid, s) for sid, s in exp["dims"][dim]["scopes"].items() if sid != "default"],
                       key=lambda x: -x[1]["priority"]) for dim in DIMS}
    cohorts, allocs = [], {"default": cfg["allocations"]["default"]}

    def merge(w1, w2):
        out = {k: list(v) for k, v in w1.items()}
        for k, v in w2.items():
            if k in out:
                inter = [x for x in out[k] if x in v]
                if not inter:
                    return None
                out[k] = inter
            else:
                out[k] = list(v)
        return out

    for sw, scw in exc["welcome_message"]:
        for sv, scv in exc["voice"]:
            w = merge(scw["where"], scv["where"])
            if w is None:
                continue
            cid = f"{OUR_COHORT_PREFIX}{sw}&{sv}"
            cohorts.append({"id": cid, "where": w})
            allocs[cid] = {"welcome_message": scope_alloc(scw), "voice": scope_alloc(scv)}
    for dim in DIMS:
        for sid, sc in exc[dim]:
            cid = f"{OUR_COHORT_PREFIX}{sid}"
            cohorts.append({"id": cid, "where": sc["where"]})
            allocs[cid] = {dim: scope_alloc(sc)}
    cfg["cohorts"] = cohorts[:20]
    keep = {c["id"] for c in cfg["cohorts"]} | {"default"}
    cfg["allocations"] = {k: v for k, v in allocs.items() if k in keep}
    hw = cfg.get("schedule", {}).get("hour_weights", {})
    for k in list(hw):
        if k != "default" and k not in keep:
            del hw[k]
    return cfg


class ExpData:
    """Read-side helper over the exp cube for one workflow and one dimension."""

    def __init__(self, agg: Aggregates, a: WfAgg, dim: str):
        self.agg, self.a, self.dim = agg, a, dim
        self.opts = a.W if dim == "welcome_message" else a.V
        self.n_codes = len(agg.codes.rows)

    def counts(self, h_from: int, h_to: int, code_mask: np.ndarray):
        """-> arrays (days, codes, n_opts) of connected and engaged, restricted to the mask."""
        a = self.a
        lo, hi = max(a.hidx(h_from), 0), max(a.hidx(h_to), 0)
        cube = a.exp[lo:hi, :self.n_codes]
        cube = cube.sum(axis=3) if self.dim == "welcome_message" else cube.sum(axis=2)  # (T, C, nO, 2)
        cube = cube[:, code_mask[:self.n_codes]]
        T = cube.shape[0]
        if T == 0:
            return None
        hours_abs = np.arange(lo, hi) + a.base
        day = hours_abs // 24
        days = np.unique(day)
        out = np.zeros((len(days), cube.shape[1], cube.shape[2], 2))
        for i, d in enumerate(days):
            out[i] = cube[day == d].sum(axis=0)
        return out

    def compare(self, h_from, h_to, mask, a_opt, c_opt):
        c = self.counts(h_from, h_to, mask)
        if c is None:
            return None
        ia, ic = self.opts.index(a_opt), self.opts.index(c_opt)
        rd, se, na, nc, pa, pc = mh_risk_difference(c[..., ia, 0], c[..., ia, 1], c[..., ic, 0], c[..., ic, 1])
        lift = rd / pc if pc > 0 else 0.0
        return {"option": a_opt, "vs": c_opt, "rd": rd, "se": se, "z": rd / se if se < float("inf") else 0.0,
                "n": na, "n_vs": nc, "rate": pa, "rate_vs": pc, "lift": lift}


def _pop_mask(agg: Aggregates, exp_dim: dict, sid: str, foreign: list[dict], dim: str) -> np.ndarray:
    """Codes whose allocation for `dim` is governed by scope `sid` (first-match by priority)."""
    m = np.ones(512, dtype=bool)
    for c in foreign:
        m &= ~agg.codes.mask(c["where"])
    scopes = sorted([(k, s) for k, s in exp_dim["scopes"].items() if k != "default"], key=lambda x: -x[1]["priority"])
    for k, s in scopes:
        sm = agg.codes.mask(s["where"])
        if k == sid:
            return m & sm
        m &= ~sm
    return m if sid == "default" else np.zeros(512, dtype=bool)


def candidate_segments(agg: Aggregates, a: WfAgg) -> list[tuple[str, dict]]:
    segs = []
    for feat in ("age_band", "lang", "city_tier", "value_band"):
        vals = agg.codes.values(feat)
        if len(vals) < 2:
            continue
        for v in vals:
            segs.append((f"{feat}={v}", {feat: [v]}))
    return segs


def plan_experiments(agg: Aggregates, a: WfAgg, exp: dict, live: dict, now_h: int, urgent_only=False):
    """Returns (new_state, decisions[list of dict], urgent: bool). `urgent_only` runs decay checks only."""
    st = copy.deepcopy(exp)
    decisions = []
    urgent = False
    foreign = foreign_cohorts(live)
    for dim in DIMS:
        ed = st["dims"][dim]
        if ed.get("cooldown_until") and now_h < ed["cooldown_until"]:
            continue
        data = ExpData(agg, a, dim)
        opts = data.opts
        # ---------------- decay / rollback for every scope with a holdout
        for sid, s in list(ed["scopes"].items()):
            if not (s.get("promo") and s.get("control")):
                continue
            mask = _pop_mask(agg, ed, sid, foreign, dim)
            w = data.compare(now_h - P.EXP_DECAY_WINDOW_H, now_h, mask, s["champion"], s["control"])
            pr = s["promo"]
            if not w or w["n_vs"] < P.EXP_DECAY_MIN_CONTROL_N:
                continue
            zd = (w["rd"] - pr["rd"]) / math.sqrt(w["se"] ** 2 + pr["se"] ** 2)
            if (zd <= P.EXP_DECAY_Z_DROP and w["rd"] < pr["rd"] / 3) or w["z"] <= -2.0:
                old = s["champion"]
                s["retired"][old] = f"decayed at hour {now_h}: lift {w['lift']:+.1%} over last {P.EXP_DECAY_WINDOW_H}h " \
                                    f"vs {pr['lift']:+.1%} at promotion (z_drop={zd:.1f})"
                s["champion"], s["control"], s["promo"] = s["control"], None, None
                s["challengers"] = [o for o in s["challengers"] if o != old]
                decisions.append({"type": "rollback", "dim": dim, "scope": sid, "from": old, "to": s["champion"],
                                  "window": _r(w), "promoted_on": pr, "z_drop": round(zd, 2)})
                urgent = True
        if urgent_only:
            continue
        # ---------------- default scope: start / evaluate
        s = ed["scopes"]["default"]
        if not s["challengers"] and s["started_hour"] is None and not s.get("promo") and not ed.get("supervisor_touched"):
            ch = [o for o in opts if o != s["champion"] and o not in s["retired"]]
            if ch:
                s["challengers"] = ch
                decisions.append({"type": "start", "dim": dim, "scope": "default", "champion": s["champion"],
                                  "challengers": ch, "alloc": scope_alloc(s)})
            continue
        if s["started_hour"] is None:
            continue
        mask = _pop_mask(agg, ed, "default", foreign, dim)
        m = max(len(s["challengers"]), 1)
        zc = z_for_alpha(P.EXP_ALPHA_WIN / m)
        results = []
        for o in list(s["challengers"]):
            r = data.compare(s["started_hour"], now_h, mask, o, s["champion"])
            if not r:
                continue
            results.append(r)
            rr = data.compare(max(s["started_hour"], now_h - P.EXP_RECENT_LOSER_H), now_h, mask, o, s["champion"])
            if r["n"] >= P.EXP_LOSER_MIN_N and r["z"] <= P.EXP_LOSER_Z:
                s["challengers"].remove(o)
                s["retired"][o] = f"worse than {s['champion']}: lift {r['lift']:+.1%}, z={r['z']:.1f}"
                decisions.append({"type": "retire_loser", "dim": dim, "scope": "default", **_r(r)})
            elif rr and rr["n"] >= P.EXP_LOSER_MIN_N and rr["z"] <= P.EXP_LOSER_Z and \
                    now_h - s["started_hour"] > P.EXP_RECENT_LOSER_H:
                s["challengers"].remove(o)
                s["retired"][o] = (f"worse than {s['champion']} over the last {P.EXP_RECENT_LOSER_H}h "
                                   f"(lift {rr['lift']:+.1%}, z={rr['z']:.1f}) although cumulative lift is {r['lift']:+.1%}"
                                   f" - looks like decay")
                decisions.append({"type": "retire_loser", "dim": dim, "scope": "default", "recent": True, **_r(rr)})
            elif (r["n"] >= P.EXP_FUTILITY_N and r["lift"] < P.EXP_MIN_LIFT) or \
                    (r["n"] >= P.EXP_HARD_CAP_N and r["z"] < zc):
                s["challengers"].remove(o)
                s["retired"][o] = f"no detectable lift after {int(r['n'])} connects (lift {r['lift']:+.1%})"
                decisions.append({"type": "retire_futile", "dim": dim, "scope": "default", **_r(r)})
        winners = [r for r in results if r["option"] in s["challengers"] and r["z"] >= zc
                   and r["n"] >= P.EXP_MIN_N_DEFAULT and r["n_vs"] >= P.EXP_MIN_N_DEFAULT and r["lift"] >= P.EXP_MIN_LIFT]
        if winners:
            best = max(winners, key=lambda r: r["z"])
            old = s["champion"]
            s["challengers"].remove(best["option"])
            s["control"], s["champion"] = old, best["option"]
            s["promo"] = {"rd": best["rd"], "se": best["se"], "lift": best["lift"], "hour": now_h}
            decisions.append({"type": "promote", "dim": dim, "scope": "default", "z_crit": round(zc, 2), **_r(best)})
            # protect segments where the new champion is worse than the old one
            for sid, where in candidate_segments(agg, a):
                if sid in ed["scopes"] or _n_exceptions(ed) >= P.EXP_MAX_EXCEPTIONS_PER_DIM or foreign:
                    continue
                sm = mask & agg.codes.mask(where)
                r = data.compare(s["started_hour"], now_h, sm, best["option"], old)
                if r and r["n"] >= P.EXP_MIN_N_SEGMENT / 2 and r["z"] <= -1.645:
                    sc = _scope(old, where=where)
                    sc["priority"] = 1.0 + abs(r["lift"])
                    sc["retired"][best["option"]] = f"worse here: lift {r['lift']:+.1%}, z={r['z']:.1f}"
                    ed["scopes"][sid] = sc
                    decisions.append({"type": "protect_segment", "dim": dim, "scope": sid, **_r(r)})
        # ---------------- segment exceptions (heterogeneous winners)
        if foreign or now_h - s["started_hour"] < P.EXP_SEGMENT_START_AFTER_H:
            continue
        segs = [(sid, w) for sid, w in candidate_segments(agg, a) if sid not in ed["scopes"]]
        pop_total = data.counts(s["started_hour"], now_h, mask)
        if pop_total is None:
            continue
        tot_conn = pop_total[..., 0].sum()
        def seg_conn(w):
            c = data.counts(s["started_hour"], now_h, mask & agg.codes.mask(w))
            return 0 if c is None else c[..., 0].sum()
        segs = [(sid, w) for sid, w in segs if seg_conn(w) >= P.EXP_SEGMENT_MIN_SHARE * tot_conn]
        fam = max(len(segs) * (len(opts) - 1), 1)
        zs = z_for_alpha(P.EXP_ALPHA_WIN / fam)
        found = []
        for sid, where in segs:
            sm = mask & agg.codes.mask(where)
            for o in opts:
                if o == s["champion"]:
                    continue
                r = data.compare(s["started_hour"], now_h, sm, o, s["champion"])
                if r and r["z"] >= zs and r["n"] >= P.EXP_MIN_N_SEGMENT and r["lift"] >= P.EXP_MIN_LIFT:
                    found.append((sid, where, r))
        found.sort(key=lambda x: -x[2]["z"])
        used = set()
        for sid, where, r in found:
            if sid in used or _n_exceptions(ed) >= P.EXP_MAX_EXCEPTIONS_PER_DIM:
                continue
            used.add(sid)
            sc = _scope(r["option"], where=where, control=s["champion"])
            sc["priority"] = r["lift"]
            sc["started_hour"] = s["started_hour"]
            sc["promo"] = {"rd": r["rd"], "se": r["se"], "lift": r["lift"], "hour": now_h}
            ed["scopes"][sid] = sc
            decisions.append({"type": "segment_winner", "dim": dim, "scope": sid, "z_crit": round(zs, 2), **_r(r)})
    return st, decisions, urgent


def _n_exceptions(ed):
    return sum(1 for k in ed["scopes"] if k != "default")


def _r(r):
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()}


def exp_dry_run(agg: Aggregates, a: WfAgg, live: dict, new: dict, now_h: int) -> dict:
    """Off-policy replay on the last 7 days of connected calls: per (cohort-population, option) observed
    engagement, shrunk toward the population mean (prior 100 connects); options never run are priced at
    the mean, i.e. we claim no lift for what we have not observed."""
    out = {}
    for dim in DIMS:
        data = ExpData(agg, a, dim)
        allm = np.zeros(512, dtype=bool)
        allm[:len(agg.codes.rows)] = True
        c = data.counts(now_h - 24 * 7, now_h, allm)
        if c is None:
            continue
        conn = c[..., 0].sum(axis=0)  # (codes, opts)
        eng = c[..., 1].sum(axis=0)
        n_codes = conn.shape[0]
        base = eng.sum() / max(conn.sum(), 1)
        rates = (eng.sum(axis=0) + 100 * base) / (conn.sum(axis=0) + 100)
        weights = conn.sum(axis=1)

        def expected(cfg):
            tot = 0.0
            for code in range(n_codes):
                dist = _dist_for_code(agg, cfg, code, dim)
                s = sum(dist.values()) or 1
                tot += weights[code] * sum(w / s * rates[data.opts.index(o)] for o, w in dist.items())
            return tot / max(weights.sum(), 1)

        eo, en = expected(live), expected(new)
        out[dim] = {"engagement_old": round(eo, 4), "engagement_new": round(en, 4), "lift": round(en / eo - 1, 4),
                    "observed_connects_by_option": {o: int(conn[:, i].sum()) for i, o in enumerate(data.opts)}}
    lift = 1.0
    for v in out.values():
        lift *= 1 + v["lift"]
    out["combined_engagement_lift"] = round(lift - 1, 4)
    out["method"] = "replay on last 7 days of connected calls, observed rates per option, unobserved options priced at mean"
    return out


def _dist_for_code(agg, cfg, code, dim):
    row = agg.codes.rows[code]
    for c in cfg.get("cohorts", []):
        if all(row[f] in v for f, v in c["where"].items()):
            return (cfg["allocations"].get(c["id"]) or {}).get(dim) or cfg["allocations"]["default"][dim]
    return cfg["allocations"]["default"][dim]


def affected_leads(agg: Aggregates, a: WfAgg, live: dict, new: dict, mid_flight: str, daily_batch: int) -> int:
    active = a.active_by_code()
    n = 0.0
    agent_changed = np.zeros(512, dtype=bool)
    for code in range(len(agg.codes.rows)):
        for dim in ("welcome_message", "voice", "caller_id"):
            if _dist_for_code(agg, live, code, dim) != _dist_for_code(agg, new, code, dim):
                agent_changed[code] = True
    if mid_flight == "next_campaign_only":
        tot = active[:len(agg.codes.rows)].sum() or 1
        n += daily_batch * float(active[agent_changed].sum()) / tot
    else:
        n += float(active[agent_changed].sum())
    if live.get("prompt_version") != new.get("prompt_version"):
        n = float(active.sum())
    if live.get("schedule") != new.get("schedule") and not agent_changed.any():
        n = max(n, len(new.get("schedule", {}).get("lead_preferred_hour", {})))
    return int(max(n, 0))


# =============================================================================== hallucination (C8)
def hal_window(a: WfAgg, h_from: int, h_to: int, prompt: str):
    lo, hi = max(a.hidx(h_from), 0), max(a.hidx(h_to), 0)
    p = a.Pi[prompt]
    w = a.hal[lo:hi, p]
    return int(w[:, 0].sum()), int(w[:, 1].sum()), int(w[:, 2].sum())


def hal_check(a: WfAgg, prompt: str, since_h: int, now_h: int, base_rate: float):
    n, f, _ = hal_window(a, max(since_h, now_h - P.HAL_WINDOW_H), now_h, prompt)
    exp = base_rate * n
    p = poisson_sf(f, exp)
    thr = P.HAL_P_THRESHOLD_AFTER_CHANGE if now_h - since_h <= 24 else P.HAL_P_THRESHOLD
    alarm = f >= P.HAL_MIN_FLAGS and p < thr and n > 0 and f / n >= P.HAL_MIN_RATIO * base_rate
    return alarm, {"connects": n, "flags": f, "expected": round(exp, 2), "p_value": p,
                   "rate": round(f / n, 4) if n else None, "base_rate": round(base_rate, 4)}


def validation_ok(run: dict, min_pass: float) -> bool:
    return run["pass_rate"] >= min_pass and run["failures"] / run["n"] <= P.HAL_MAX_FAILURE_RATE
