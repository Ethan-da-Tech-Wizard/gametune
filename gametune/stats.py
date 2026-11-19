# ==============================================================================
# Program: stats.py
# Programmer: Ethan-da-Tech-Wizard
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
# Only the standard library is needed: percentiles and variance are simple
# enough that numpy would be a heavy dependency for no gain.
import math

# Verdict labels. These exact strings are shown to the user and stored in the
# JSON report, and the tests compare against them, so treat them as an API.
IMPROVED = "IMPROVED"
NO_CHANGE = "NO MEASURABLE CHANGE"
WORSE = "WORSE"
NOT_ENOUGH = "NOT ENOUGH DATA"


def percentile(sorted_values, p):
    """Return the p-th percentile (0-100) of an ascending list.

    Uses linear interpolation between the two nearest ranks (the same
    convention as numpy's default), so results are smooth for small samples.
    Raises ValueError on an empty list.
    """
    # Guard: there is no sensible percentile of nothing, and failing loudly is
    # better than silently reporting 0.
    if not sorted_values:
        raise ValueError("no values")
    # Fractional index into the sorted list; floor/ceil give the two neighbours.
    k = (len(sorted_values) - 1) * p / 100.0
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    # Exact hit on a sample, no interpolation needed.
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def _low(sorted_desc_times, fraction):
    """FPS equivalent of the slowest `fraction` of frames.

    Input must be frame times in ms sorted slowest-first. At least one frame is
    always used so tiny logs still give a defined number.
    """
    # Round up so 1% of 150 frames means 2 frames, never 0.
    n = max(1, int(math.ceil(len(sorted_desc_times) * fraction)))
    worst = sorted_desc_times[:n]
    # Mean frame time of the worst frames, converted ms -> frames per second.
    return 1000.0 / (sum(worst) / len(worst))


def run_metrics(frametimes_ms):
    """Summarise one benchmark run from its per-frame times in milliseconds.

    Returns a dict with frame count, duration, average/median FPS, 1% and
    0.1% low FPS and the p99 frame time. Raises ValueError for an empty run.
    """
    n = len(frametimes_ms)
    if n == 0:
        raise ValueError("no frames")
    # Average FPS is frames / total time (not the mean of per-frame FPS), so a
    # long stall is weighted by how long it actually lasted.
    total_ms = sum(frametimes_ms)
    asc = sorted(frametimes_ms)
    # Slowest-first copy for the "low" metrics.
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
    """Arithmetic mean of a non-empty list."""
    return sum(v) / len(v)


def _var(v):
    """Sample variance (n-1 denominator); 0.0 when fewer than two values."""
    if len(v) < 2:
        return 0.0
    m = _mean(v)
    return sum((x - m) ** 2 for x in v) / (len(v) - 1)


# Practical-significance floor, as a fraction of the baseline mean. Statistical
# significance alone is not enough: with near-zero noise a 0.1% shift would
# otherwise be reported as a win.
MIN_PRACTICAL = 0.01   # a change under 1% is not worth calling, even if the noise is tiny


def compare_metric(base, tuned, higher_is_better):
    """Compare two lists of per-run values. Returns a dict with a direction of better/worse/none."""
    # Per-run values are treated as independent samples of each configuration.
    mb, mt = _mean(base), _mean(tuned)
    diff = mt - mb
    # Standard error of the difference of two means (unpooled / Welch style).
    se = math.sqrt(_var(base) / len(base) + _var(tuned) / len(tuned))
    pct = (diff / mb * 100.0) if mb else 0.0
    # Practical test: the change must be at least 1% of the baseline mean.
    big_enough = abs(diff) >= MIN_PRACTICAL * abs(mb) if mb else diff != 0
    # Statistical test: difference must exceed two standard errors (roughly 95%).
    beyond_noise = abs(diff) > 2.0 * se
    direction = "none"
    if big_enough and beyond_noise and diff != 0:
        # FPS metrics improve upward; frame-time metrics improve downward.
        better = diff > 0 if higher_is_better else diff < 0
        direction = "better" if better else "worse"
    return {"baseline_mean": mb, "tuned_mean": mt, "difference": diff, "percent": pct,
            "std_error": se, "two_std_errors": 2.0 * se, "direction": direction}


def compare(base_runs, tuned_runs, min_runs=3):
    """base_runs / tuned_runs: lists of run_metrics dicts. Returns (verdict, per_metric dict, explanation)."""
    # Hard floor of 3 runs: callers may ask for more but never fewer, because one
    # or two runs cannot estimate run-to-run variation.
    min_runs = max(3, int(min_runs))
    if len(base_runs) < min_runs or len(tuned_runs) < min_runs:
        return NOT_ENOUGH, {}, (
            "I need at least %d usable runs on each side before I will say anything. I have %d baseline and %d tuned. "
            "A single run cannot tell a real change from normal run-to-run variation, so no claim is made."
            % (min_runs, len(base_runs), len(tuned_runs)))
    # Three headline metrics: throughput, stutter (1% low) and tail latency (p99).
    metrics = {
        "avg_fps": compare_metric([r["avg_fps"] for r in base_runs], [r["avg_fps"] for r in tuned_runs], True),
        "low1_fps": compare_metric([r["low1_fps"] for r in base_runs], [r["low1_fps"] for r in tuned_runs], True),
        "p99_ms": compare_metric([r["p99_ms"] for r in base_runs], [r["p99_ms"] for r in tuned_runs], False),
    }
    better = [k for k, v in metrics.items() if v["direction"] == "better"]
    worse = [k for k, v in metrics.items() if v["direction"] == "worse"]
    labels = {"avg_fps": "average FPS", "low1_fps": "1% low FPS", "p99_ms": "99th percentile frame time"}
    fmt = lambda ks: ", ".join(labels[k] for k in ks)
    # Mixed outcomes are deliberately NOT reported as a win; the honest verdict
    # is "no measurable change" with an explanation.
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
