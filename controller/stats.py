"""Small, deterministic statistics helpers. No randomness anywhere in winner selection."""
from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np

_N = NormalDist()


def z_for_alpha(alpha: float) -> float:
    """One-sided critical value."""
    return _N.inv_cdf(1 - alpha)


def poisson_sf(k: int, lam: float) -> float:
    """P(X >= k) for X ~ Poisson(lam)."""
    if k <= 0:
        return 1.0
    if lam <= 0:
        return 0.0
    # 1 - P(X <= k-1), summed in log space
    s, term = 0.0, math.exp(-lam)
    for i in range(k):
        s += term
        term *= lam / (i + 1)
    return max(0.0, 1.0 - s)


def wilson_upper(fails: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 1.0
    p = fails / n
    den = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c + r) / den


def mh_risk_difference(na, ea, nc, ec):
    """Stratified (Mantel-Haenszel weighted) difference in engagement rate, arm a minus control c.

    Inputs are arrays over strata (e.g. day x feature-cell). Strata where either arm has no data drop out.
    Returns (rd, se, n_a, n_c, p_a, p_c) with pooled rates for reporting.
    """
    na, ea, nc, ec = (np.asarray(x, dtype=float).ravel() for x in (na, ea, nc, ec))
    ok = (na > 0) & (nc > 0)
    if not ok.any():
        return 0.0, float("inf"), float(na.sum()), float(nc.sum()), _rate(ea, na), _rate(ec, nc)
    na, ea, nc, ec = na[ok], ea[ok], nc[ok], ec[ok]
    w = na * nc / (na + nc)
    W = w.sum()
    rd = float((w * (ea / na - ec / nc)).sum() / W)
    pbar = (ea.sum() + ec.sum()) / (na.sum() + nc.sum())
    var = float((w ** 2 * pbar * (1 - pbar) * (1 / na + 1 / nc)).sum() / W ** 2)
    return rd, math.sqrt(max(var, 1e-12)), float(na.sum()), float(nc.sum()), _rate(ea, na), _rate(ec, nc)


def _rate(e, n):
    s = float(np.sum(n))
    return float(np.sum(e)) / s if s else 0.0


def observed_vs_expected(conn_d, dials_d, conn_o, dials_o):
    """Indirect standardisation of a caller ID's pickup against the other caller IDs, stratum by stratum
    (strata = hours, so the population and time-of-day mix match). Returns (ratio, z, O, E, dials)."""
    conn_d, dials_d, conn_o, dials_o = (np.asarray(x, dtype=float) for x in (conn_d, dials_d, conn_o, dials_o))
    ok = dials_o > 0
    E = float((dials_d[ok] * conn_o[ok] / dials_o[ok]).sum())
    O = float(conn_d[ok].sum())
    if E <= 0:
        return 1.0, 0.0, O, E, float(dials_d.sum())
    return O / E, (O - E) / math.sqrt(E), O, E, float(dials_d[ok].sum())


def quantize(dist: dict, q: int = 1024) -> dict:
    """Shares as exact multiples of 1/q summing to exactly 1.0 in binary floating point. The simulator
    re-normalises (w / sum) on every write; dyadic shares make that idempotent, so re-sending an unchanged
    distribution never registers as a 'changed dimension' (which would break auto-apply)."""
    items = [(k, max(float(v), 0.0)) for k, v in dist.items()]
    s = sum(v for _, v in items) or 1.0
    raw = [(k, v / s * q) for k, v in items]
    base = {k: int(math.floor(x)) for k, x in raw}
    rem = q - sum(base.values())
    for k, _ in sorted(raw, key=lambda kv: -(kv[1] - math.floor(kv[1])))[:rem]:
        base[k] += 1
    return {k: n / q for k, n in sorted(base.items()) if n > 0}


def tv_distance(a: dict, b: dict) -> float:
    keys = set(a) | set(b)
    sa, sb = sum(a.values()) or 1, sum(b.values()) or 1
    return 0.5 * sum(abs(a.get(k, 0) / sa - b.get(k, 0) / sb) for k in keys)


def step_towards(cur: dict, target: dict, max_tv: float) -> dict:
    """Move distribution `cur` toward `target` by at most `max_tv` total-variation distance."""
    sa = sum(cur.values()) or 1
    cur = {k: v / sa for k, v in cur.items()}
    d = tv_distance(cur, target)
    if d <= max_tv + 1e-9:
        return quantize(target)
    f = (max_tv - 0.003) / d  # margin for quantisation
    keys = set(cur) | set(target)
    out = {k: cur.get(k, 0) + f * (target.get(k, 0) - cur.get(k, 0)) for k in keys}
    return quantize({k: v for k, v in out.items() if v > 1e-6})
