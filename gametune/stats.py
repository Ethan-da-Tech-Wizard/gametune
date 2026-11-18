# Program: gametune - stats.py
# Programmer: Ethan-da-Tech-Wizard (written with Claude, an AI assistant)
# Last Modified: 2026-11-18
# Purpose: Computes frame-time statistics (average FPS, median, 1% and 0.1% lows, 99th percentile) and the noise-aware rule that decides if a tuned run is IMPROVED, WORSE or NO CHANGE.

"""Frame statistics and the noise-aware comparison rule. Plain python, no numpy.

Definitions (stated in every report):
  average FPS : frames / total seconds (time-weighted, so a long stall counts as it should).
  median FPS  : 1000 / median frame time.
  1% low FPS  : 1000 / (average of the slowest 1% of frames). 0.1% low uses the slowest 0.1%.
  p99 frame time : the 99th percentile frame time in ms (lower is better).
"""
# math gives floor, ceil and sqrt.
import math

# The four possible verdict texts.
IMPROVED = "IMPROVED"
NO_CHANGE = "NO MEASURABLE CHANGE"
WORSE = "WORSE"
NOT_ENOUGH = "NOT ENOUGH DATA"


def percentile(sorted_values, p):
    """Return the p-th percentile of an already sorted list (p from 0 to 100)."""
    # Linear interpolation between neighbouring sorted values.
    if not sorted_values:
        # Cannot take a percentile of nothing.
        raise ValueError("no values")
    # Position in the sorted list; fractional positions fall between two values.
    k = (len(sorted_values) - 1) * p / 100.0
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    # Exactly on one value: use it.
    if lo == hi:
        return sorted_values[lo]
    # Otherwise blend the two neighbours by the fractional part.
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def _low(sorted_desc_times, fraction):
    """Return the FPS equivalent of the slowest `fraction` of frames (a '1% low' when fraction is 0.01)."""
    # How many frames are the slowest `fraction` (at least one).
    n = max(1, int(math.ceil(len(sorted_desc_times) * fraction)))
    # Frames are sorted slowest first, so take the first n.
    worst = sorted_desc_times[:n]
    # Convert the average of those slow frame times (ms) into FPS.
    return 1000.0 / (sum(worst) / len(worst))


def run_metrics(frametimes_ms):
    """Compute the summary numbers for one run from its frame times in milliseconds."""
    n = len(frametimes_ms)
    # No frames, no stats.
    if n == 0:
        raise ValueError("no frames")
    # Total time is the sum of the frame times.
    total_ms = sum(frametimes_ms)
    # Sorted fastest-to-slowest and reverse.
    asc = sorted(frametimes_ms)
    desc = asc[::-1]
    # Average FPS is frames divided by total seconds (time-weighted).
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
    """Return the average of a list of numbers."""
    # Plain average.
    return sum(v) / len(v)


def _var(v):
    """Return the sample variance of a list (0 when fewer than two values)."""
    # Variance needs at least two values.
    if len(v) < 2:
        return 0.0
    # Sample variance (divides by n-1).
    m = _mean(v)
    return sum((x - m) ** 2 for x in v) / (len(v) - 1)


MIN_PRACTICAL = 0.01   # a change under 1% is not worth calling, even if the noise is tiny


def compare_metric(base, tuned, higher_is_better):
    # Averages for each side, and their difference.
    """Compare two lists of per-run values. Returns a dict with a direction of better/worse/none."""
    mb, mt = _mean(base), _mean(tuned)
    diff = mt - mb
    # Standard error of the difference between two means.
    se = math.sqrt(_var(base) / len(base) + _var(tuned) / len(tuned))
    # Change as a percentage of the baseline.
    pct = (diff / mb * 100.0) if mb else 0.0
    # The change must be at least 1% of baseline.
    big_enough = abs(diff) >= MIN_PRACTICAL * abs(mb) if mb else diff != 0
    # And more than 2 standard errors, i.e. bigger than normal run-to-run noise.
    beyond_noise = abs(diff) > 2.0 * se
    direction = "none"
    # Only call it better or worse when both tests pass.
    if big_enough and beyond_noise and diff != 0:
        # For FPS higher is better; for frame time lower is better.
        better = diff > 0 if higher_is_better else diff < 0
        direction = "better" if better else "worse"
    return {"baseline_mean": mb, "tuned_mean": mt, "difference": diff, "percent": pct,
            "std_error": se, "two_std_errors": 2.0 * se, "direction": direction}


def compare(base_runs, tuned_runs, min_runs=3):
    """base_runs / tuned_runs: lists of run_metrics dicts. Returns (verdict, per_metric dict, explanation)."""
    # Never allow fewer than 3 runs per side.
    min_runs = max(3, int(min_runs))
    # Not enough data: make no claim.
    if len(base_runs) < min_runs or len(tuned_runs) < min_runs:
        return NOT_ENOUGH, {}, (
            "I need at least %d usable runs on each side before I will say anything. I have %d baseline and %d tuned. "
            "A single run cannot tell a real change from normal run-to-run variation, so no claim is made."
            % (min_runs, len(base_runs), len(tuned_runs)))
    # Compare average FPS, 1% low FPS (higher better) and 99th percentile frame time (lower better).
    metrics = {
        "avg_fps": compare_metric([r["avg_fps"] for r in base_runs], [r["avg_fps"] for r in tuned_runs], True),
        "low1_fps": compare_metric([r["low1_fps"] for r in base_runs], [r["low1_fps"] for r in tuned_runs], True),
        "p99_ms": compare_metric([r["p99_ms"] for r in base_runs], [r["p99_ms"] for r in tuned_runs], False),
    }
    # Which metrics improved or got worse.
    better = [k for k, v in metrics.items() if v["direction"] == "better"]
    worse = [k for k, v in metrics.items() if v["direction"] == "worse"]
    labels = {"avg_fps": "average FPS", "low1_fps": "1% low FPS", "p99_ms": "99th percentile frame time"}
    # Turn metric keys into readable names.
    fmt = lambda ks: ", ".join(labels[k] for k in ks)
    # Mixed results are not a clear win.
    if better and worse:
        return NO_CHANGE, metrics, ("Mixed result: %s got better but %s got worse, each by more than the noise. "
                                    "That is not a clear win, so no gain is claimed." % (fmt(better), fmt(worse)))
    # Any clear worsening is reported as WORSE.
    if worse:
        return WORSE, metrics, "%s got worse by more than twice the run-to-run noise." % fmt(worse).capitalize()
    # Only clear improvements, with no worsening.
    if better:
        return IMPROVED, metrics, ("%s improved by more than twice the run-to-run noise (and by at least 1%%). "
                                   "This is true for this game, this scene and this PC only." % fmt(better).capitalize())
    # Nothing beat the noise threshold.
    return NO_CHANGE, metrics, ("The difference is smaller than twice the run-to-run noise, or smaller than 1%. "
                                "That means the change could not be told apart from normal variation.")
