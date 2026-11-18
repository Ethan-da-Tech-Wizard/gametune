# ==============================================================================
# Program: stats.py
# Programmer: Ethan-da-Tech-Wizard
# Course: CIS 156 / Advanced Systems
# Last Modified: November 18, 2026
# Purpose: Statistical distribution calculations including frame-time percentiles, 1% lows, and smoothness variance.
# ==============================================================================

"""Frame statistics and the noise-aware comparison rule. Plain python, no numpy.

Definitions (stated in every report):
  average FPS : frames / total seconds (time-weighted, so a long stall counts as it should).
  median FPS  : 1000 / median frame time.
  1% low FPS  : 1000 / (average of the slowest 1% of frames). 0.1% low uses the slowest 0.1%.
  p99 frame time : the 99th percentile frame time in ms (lower is better).
"""
import math

IMPROVED = "IMPROVED"
NO_CHANGE = "NO MEASURABLE CHANGE"
WORSE = "WORSE"
NOT_ENOUGH = "NOT ENOUGH DATA"


def percentile(sorted_values, p):
    if not sorted_values:
        raise ValueError("no values")
    k = (len(sorted_values) - 1) * p / 100.0
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def _low(sorted_desc_times, fraction):
    n = max(1, int(math.ceil(len(sorted_desc_times) * fraction)))
    worst = sorted_desc_times[:n]
    return 1000.0 / (sum(worst) / len(worst))


def run_metrics(frametimes_ms):
    n = len(frametimes_ms)
    if n == 0:
        raise ValueError("no frames")
    total_ms = sum(frametimes_ms)
    asc = sorted(frametimes_ms)
    desc = asc[::-1]
    return {
        "frames": n,
        "seconds": total_ms / 1000.0,
        "avg_fps": 1000.0 * n / total_ms,
        "median_fps": 1000.0 / percentile(asc, 50),
        "low1_fps": _low(desc, 0.01),
        "low01_fps": _low(desc, 0.001),
        "p99_ms": percentile(asc, 99),
    }


def _mean(v):
    return sum(v) / len(v)


def _var(v):
    if len(v) < 2:
        return 0.0
    m = _mean(v)
    return sum((x - m) ** 2 for x in v) / (len(v) - 1)


MIN_PRACTICAL = 0.01   # a change under 1% is not worth calling, even if the noise is tiny


def compare_metric(base, tuned, higher_is_better):
    """Compare two lists of per-run values. Returns a dict with a direction of better/worse/none."""
    mb, mt = _mean(base), _mean(tuned)
    diff = mt - mb
    se = math.sqrt(_var(base) / len(base) + _var(tuned) / len(tuned))
    pct = (diff / mb * 100.0) if mb else 0.0
    big_enough = abs(diff) >= MIN_PRACTICAL * abs(mb) if mb else diff != 0
    beyond_noise = abs(diff) > 2.0 * se
    direction = "none"
    if big_enough and beyond_noise and diff != 0:
        better = diff > 0 if higher_is_better else diff < 0
        direction = "better" if better else "worse"
    return {"baseline_mean": mb, "tuned_mean": mt, "difference": diff, "percent": pct,
            "std_error": se, "two_std_errors": 2.0 * se, "direction": direction}


def compare(base_runs, tuned_runs, min_runs=3):
    """base_runs / tuned_runs: lists of run_metrics dicts. Returns (verdict, per_metric dict, explanation)."""
    min_runs = max(3, int(min_runs))
    if len(base_runs) < min_runs or len(tuned_runs) < min_runs:
        return NOT_ENOUGH, {}, (
            "I need at least %d usable runs on each side before I will say anything. I have %d baseline and %d tuned. "
            "A single run cannot tell a real change from normal run-to-run variation, so no claim is made."
            % (min_runs, len(base_runs), len(tuned_runs)))
    metrics = {
        "avg_fps": compare_metric([r["avg_fps"] for r in base_runs], [r["avg_fps"] for r in tuned_runs], True),
        "low1_fps": compare_metric([r["low1_fps"] for r in base_runs], [r["low1_fps"] for r in tuned_runs], True),
        "p99_ms": compare_metric([r["p99_ms"] for r in base_runs], [r["p99_ms"] for r in tuned_runs], False),
    }
    better = [k for k, v in metrics.items() if v["direction"] == "better"]
    worse = [k for k, v in metrics.items() if v["direction"] == "worse"]
    labels = {"avg_fps": "average FPS", "low1_fps": "1% low FPS", "p99_ms": "99th percentile frame time"}
    fmt = lambda ks: ", ".join(labels[k] for k in ks)
    if better and worse:
        return NO_CHANGE, metrics, ("Mixed result: %s got better but %s got worse, each by more than the noise. "
                                    "That is not a clear win, so no gain is claimed." % (fmt(better), fmt(worse)))
    if worse:
        return WORSE, metrics, "%s got worse by more than twice the run-to-run noise." % fmt(worse).capitalize()
    if better:
        return IMPROVED, metrics, ("%s improved by more than twice the run-to-run noise (and by at least 1%%). "
                                   "This is true for this game, this scene and this PC only." % fmt(better).capitalize())
    return NO_CHANGE, metrics, ("The difference is smaller than twice the run-to-run noise, or smaller than 1%. "
                                "That means the change could not be told apart from normal variation.")
