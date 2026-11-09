"""Unit tests for the controller's pure decision helpers. Run: python -m pytest -q tests"""
import numpy as np

from controller import params as P
from controller import policy as pol
from controller.aggregates import CodeBook
from controller.stats import mh_risk_difference, poisson_sf, quantize, step_towards, tv_distance


def test_quantize_is_exact_and_idempotent():
    q = quantize({"V1": 0.5, "V2": 1 / 6, "V3": 1 / 6, "V4": 1 / 6})
    assert sum(q.values()) == 1.0
    s = sum(q.values())
    assert {k: v / s for k, v in q.items()} == q  # the simulator's w/sum renormalisation is a no-op


def test_step_towards_respects_max_shift():
    cur, tgt = {"D1": 0.5, "D2": 0.5}, {"D1": 0.5, "D3": 0.5}
    out = step_towards(cur, tgt, 0.2)
    assert tv_distance(cur, out) <= 0.2 + 1e-9
    assert tv_distance(out, tgt) < tv_distance(cur, tgt)


def test_mh_risk_difference_removes_stratum_confounding():
    # arm a is only run in a high-engagement stratum on day 2; true within-stratum difference is 0
    na, ea = np.array([100, 900]), np.array([50, 720])
    nc, ec = np.array([900, 100]), np.array([450, 80])
    rd, se, *_ = mh_risk_difference(na, ea, nc, ec)
    assert abs(rd) < 1e-9  # a pooled comparison would claim +0.24


def test_poisson_tail():
    assert abs(poisson_sf(0, 3.0) - 1.0) < 1e-12
    assert poisson_sf(12, 3.0) < 1e-4


def test_scope_alloc_shares():
    s = pol._scope("W1", ["W2", "W3"])
    a = pol.scope_alloc(s)
    assert a["W1"] == 0.5 and abs(a["W2"] - 0.25) < 1e-3
    s2 = pol._scope("W3", [], control="W1")
    a2 = pol.scope_alloc(s2)
    assert abs(a2["W1"] - P.EXP_CONTROL_SHARE) < 1e-3 and sum(a2.values()) == 1.0


def test_compile_config_pair_cohorts_first():
    live = {"cohorts": [], "allocations": {"default": {"welcome_message": {"W1": 1}, "voice": {"V1": 1},
                                                         "caller_id": {"D1": 1}}},
            "prompt_version": "P1", "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}}
    exp = pol.new_exp_state(live)
    exp["dims"]["welcome_message"]["scopes"]["lang=hi"] = pol._scope("W2", where={"lang": ["hi"]})
    exp["dims"]["voice"]["scopes"]["age_band=60+"] = pol._scope("V3", where={"age_band": ["60+"]})
    cfg = pol.compile_config(live, exp)
    ids = [c["id"] for c in cfg["cohorts"]]
    assert ids[0] == "fx:lang=hi&age_band=60+"
    assert cfg["allocations"][ids[0]] == {"welcome_message": {"W2": 1.0}, "voice": {"V3": 1.0}}
    assert set(ids[1:]) == {"fx:lang=hi", "fx:age_band=60+"}


def test_supervisor_cohorts_are_never_overwritten():
    live = {"cohorts": [{"id": "senior_ta", "where": {"age_band": ["60+"]}}],
            "allocations": {"default": {"welcome_message": {"W1": 1}, "voice": {"V1": 1}, "caller_id": {"D1": 1}},
                            "senior_ta": {"voice": {"V3": 1}}},
            "prompt_version": "P1", "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}}
    exp = pol.new_exp_state(live)
    exp["dims"]["voice"]["scopes"]["lang=hi"] = pol._scope("V2", where={"lang": ["hi"]})
    cfg = pol.compile_config(live, exp)
    assert cfg["cohorts"] == live["cohorts"] and cfg["allocations"]["senior_ta"] == {"voice": {"V3": 1}}


def test_codebook_mask():
    cb = CodeBook()
    a = cb.code({"lang": "hi", "city_tier": "1", "age_band": "60+", "value_band": "low"})
    b = cb.code({"lang": "en", "city_tier": "1", "age_band": "60+", "value_band": "low"})
    m = cb.mask({"lang": ["hi"]})
    assert m[a] and not m[b]


def test_validation_bar_separates_safe_from_unsafe():
    ok = {"failures": 10, "n": 800, "pass_rate": 1 - 10 / 800}       # ~1.25%: production-like
    bad = {"failures": 22, "n": 800, "pass_rate": 1 - 22 / 800}      # ~2.75%: persuasive-like
    assert pol.validation_ok(ok, 0.97) and not pol.validation_ok(bad, 0.97)
