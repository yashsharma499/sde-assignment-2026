"""Every tunable threshold in one place, with the reason it has the value it has.

Nothing here is fitted to a public scenario's hidden truth. Values come from sample-size arithmetic
(see SUBMISSION.md §5) or from the stakeholder memos / workflow policies.
"""

TICK_MINUTES = 15                 # advance granularity: lets us apply approvals well inside a 2h apply window
CHECKPOINT_EVERY_TICKS = 4        # snapshot ingest-derived aggregates every simulated hour

# ---------------------------------------------------------------- caller ID health (C5)
DID_PROBE_SHARE_AUTO = 0.03       # share per unknown DID when we can retreat within the hour (auto-apply)
DID_REPROBE_SHARE = 0.02          # a number we already quarantined once is re-tested at a smaller share
DID_PROBE_SHARE_APPROVAL = 0.10   # share per unknown DID when every move costs an approval
DID_MIN_DIALS_JUDGE = 150         # dials before an unknown DID can be judged healthy
DID_HEALTHY_LB = 0.80             # lower 95% bound of observed/expected pickup to call a DID healthy
DID_DEGRADED_RATIO = 0.70         # point estimate below this ... (volume load alone moves a number to ~0.85x
DID_DEGRADED_Z = -2.5             # ... and this significant => quarantine   of a fresh one; flagging is <0.6x)
DID_MIN_HEALTHY = 2               # never collapse traffic onto fewer than 2 numbers (that *creates* load) ...
DID_CATASTROPHIC_RATIO = 0.50     # ... unless the number is this bad
DID_FAST_WINDOW_H = 3             # fast path for sudden spam-flagging
DID_FAST_RATIO = 0.60
DID_FAST_Z = -3.5
DID_SLOW_WINDOW_H = 24
DID_REPROBE_AFTER_H = 72          # quarantined numbers are re-probed after 3 days, then 6, 12... (backoff)
DID_MIN_TV_TO_PROPOSE = 0.05      # don't spend an approval on a tiny rebalance

# ---------------------------------------------------------------- call time (C4)
SCHED_PRIOR_STRENGTH = 60.0       # connect-equivalents shrinking a (age,tier) cell's hour index to its age band
SCHED_MIN_PEAK_INDEX = 1.08       # below this a cell's profile is treated as flat -> no preference
SCHED_TOP_BAND = 0.10             # hours within 0.10 of the best index are all "good" (spreads capacity)

# ---------------------------------------------------------------- experiments (C6/C7)
EXP_CHAMPION_SHARE = 0.5          # incumbent keeps half the traffic while challengers are tested
EXP_CONTROL_SHARE = 0.2           # after promotion the old champion stays on 20% as a holdout for decay
EXP_ALPHA_WIN = 0.005             # one-sided family-wise alpha per daily look (Bonferroni inside the family);
                                  # ~8 looks/run keeps the overall false-winner rate around 2-3%
EXP_MIN_LIFT = 0.02               # practical significance: ignore lifts under 2% relative
EXP_MIN_N_DEFAULT = 300           # connects per arm before a workflow-wide winner can be declared
EXP_MIN_N_SEGMENT = 150           # connects per arm inside a segment
EXP_LOSER_Z = -2.0                # one-sided "worse than champion" => retire (safety action, lower bar)
EXP_LOSER_MIN_N = 100
EXP_FUTILITY_N = 2000             # connects on an arm whose estimated lift is < EXP_MIN_LIFT => retire
EXP_HARD_CAP_N = 6000             # ... or with any estimate once it has this much data and still no win
EXP_RECENT_LOSER_H = 36           # a challenger significantly worse over the last 36h is retired even if
                                  # its cumulative record is fine (catches challengers that decay mid-test)
EXP_SEGMENT_MIN_SHARE = 0.08      # segments under 8% of connects are not analysed on their own
EXP_SEGMENT_START_AFTER_H = 36    # segment analysis waits for 36h of exploration data
EXP_MAX_EXCEPTIONS_PER_DIM = 3    # 3x3 pair cohorts + 6 singles = 15 <= 20 cohorts
EXP_DECAY_WINDOW_H = 36
EXP_DECAY_MIN_CONTROL_N = 80
EXP_DECAY_Z_DROP = -2.0           # window lift significantly below the lift we promoted on
EXP_SUPERVISOR_COOLDOWN_H = 48    # don't touch a dimension a supervisor just edited

# ---------------------------------------------------------------- hallucination incidents (C8)
HAL_WINDOW_H = 4
HAL_MIN_FLAGS = 6
HAL_P_THRESHOLD = 1e-4            # Poisson tail vs the workflow's historical flag rate (steady state)
HAL_P_THRESHOLD_AFTER_CHANGE = 1e-3   # in the 24h after a prompt change the prior of a problem is higher
HAL_MAX_FAILURE_RATE = 0.0175     # validation failure rate bar: halfway between normal (~1%) and unsafe (>=2.5%)
HAL_MIN_RATIO = 2.0
HAL_VALIDATION_N = 800            # 8 credits; at n=800 a 2.75%-failure prompt passes the 1.75% bar ~5% of
                                  # the time, a 1%-failure prompt fails it ~2% of the time
HAL_VERIFY_MIN_CONNECTS = 300
HAL_CREDIT_CAP_PER_INCIDENT = 30
HAL_CREDIT_FLOOR = 12             # below this we only validate the last-known-good prompt, no LLM

# ---------------------------------------------------------------- governance (C9)
FATIGUE_RESERVE_FOR_URGENT = 1    # routine proposals leave one slot per 24h for incidents/rollbacks
URGENT_KINDS = ("incident", "rollback", "quarantine")
LATEST_ROUTINE_BEFORE_END_H = 3   # don't file routine requests that would be decided after hours

# Stakeholder preferences that are stricter than the signed policy. Keyed by tenant_id; they only apply
# when that tenant exists. (stakeholders/04: Kestrel ops wants <=2 requests/day and none before 11:00.)
TENANT_PREFS = {
    "kestrel_finance": {"max_routine_per_24h": 2, "earliest_request_hour": 11},
}
