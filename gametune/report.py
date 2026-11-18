# Program: gametune - report.py
# Programmer: Ethan-da-Tech-Wizard (written with Claude, an AI assistant)
# Last Modified: 2026-11-18
# Purpose: Loads baseline and tuned MangoHud logs, calculates per-run metrics, asks stats.py for a verdict, and builds, prints and saves the before/after report.

"""Build, print and save a before/after report from real MangoHud logs."""
# datetime stamps reports, glob finds csv files, json saves, os handles paths.
import datetime
import glob
import json
import os

# Our own modules: log parser, data folder paths and statistics.
from . import mangohud, paths, stats

# A run shorter than this (after warm-up) is thrown out.
MIN_RUN_SECONDS = 10.0
# Same for a run with too few frames.
MIN_RUN_FRAMES = 100


def expand(items):
    """Files stay files; a folder becomes its *.csv frame logs (summary files skipped); sorted for stability."""
    out = []
    # Go through everything the user listed.
    for item in items:
        # A folder stands for all frame-log CSV files inside it.
        if os.path.isdir(item):
            out.extend(sorted(p for p in glob.glob(os.path.join(item, "*.csv")) if not p.lower().endswith("_summary.csv")))
        # A plain file is kept as is.
        else:
            out.append(item)
    return out


def load_side(files, warmup_s, min_seconds, min_frames):
    """Read every log file on one side and return (run metrics list, notes list, system info dictionary)."""
    # runs: usable per-run numbers; notes: why anything was skipped.
    runs, notes = [], []
    # context: the system header from the first usable log, for fairness checks.
    context = {}
    for path in files:
        # Try to parse each log; a bad file is skipped with a note, not a crash.
        try:
            log = mangohud.parse_file(path)
        except mangohud.LogError as exc:
            notes.append("Skipped %s: %s" % (os.path.basename(path), exc))
            continue
        # Throw away the warm-up seconds.
        frames = mangohud.drop_warmup(log, warmup_s)
        # Seconds of play left after the warm-up.
        seconds = sum(frames) / 1000.0
        # Reject runs that are too short to trust.
        if len(frames) < min_frames or seconds < min_seconds:
            notes.append("Skipped %s: only %d frames and %.1f seconds are left after throwing away the first %.0f seconds. "
                         "At least %d frames and %.0f seconds are needed." % (
                             os.path.basename(path), len(frames), seconds, warmup_s, min_frames, min_seconds))
            continue
        # Compute the run's statistics and attach file name and peak temperatures.
        m = stats.run_metrics(frames)
        m["file"] = os.path.basename(path)
        m["max_gpu_temp_c"] = max(log.gpu_temps) if log.gpu_temps else None
        m["max_cpu_temp_c"] = max(log.cpu_temps) if log.cpu_temps else None
        # Tell the user about damaged rows.
        if log.skipped_rows:
            notes.append("%s: ignored %d damaged or cut-off row(s)." % (os.path.basename(path), log.skipped_rows))
        # Save the first log's system info (GPU, CPU, driver, kernel).
        if not context:
            context = dict(log.header)
        runs.append(m)
    return runs, notes, context


def build(baseline_files, tuned_files, warmup_s=5.0, min_runs=3, min_seconds=MIN_RUN_SECONDS, min_frames=MIN_RUN_FRAMES):
    """Compare two sides and return the full report dictionary (verdict, metrics, warnings, notes)."""
    # Load both sides the same way.
    b_runs, b_notes, b_ctx = load_side(baseline_files, warmup_s, min_seconds, min_frames)
    t_runs, t_notes, t_ctx = load_side(tuned_files, warmup_s, min_seconds, min_frames)
    # stats.compare decides the verdict from the per-run numbers.
    verdict, metrics, why = stats.compare(b_runs, t_runs, min_runs)
    warnings = []
    # Warn if the two sides ran on different hardware or drivers, which makes the comparison unfair.
    for key in ("gpu", "cpu", "driver", "kernel"):
        if b_ctx.get(key) and t_ctx.get(key) and b_ctx[key] != t_ctx[key]:
            warnings.append("The %s differs between baseline and tuned logs (%s vs %s). The comparison may not be fair." % (key, b_ctx[key], t_ctx[key]))
    # Runs where the GPU reached 86 C or more.
    hot = [r["file"] for r in b_runs + t_runs if r["max_gpu_temp_c"] is not None and r["max_gpu_temp_c"] >= 86]
    if hot:
        warnings.append("The GPU reached 86 C or more during: %s. A hot GPU slows itself down, which can distort results and is unsafe to push." % ", ".join(hot))
    # The finished report as a plain dictionary (also used for JSON).
    return {
        "created": datetime.datetime.now().replace(microsecond=0).isoformat(),
        "verdict": verdict,
        "explanation": why,
        "warmup_seconds_discarded": warmup_s,
        "baseline_runs": b_runs,
        "tuned_runs": t_runs,
        "metrics": metrics,
        "notes": b_notes + t_notes,
        "warnings": warnings,
        "context": {"baseline": b_ctx, "tuned": t_ctx},
        "definitions": "1% low FPS = 1000 / average of the slowest 1% of frames. p99 = 99th percentile frame time. "
                       "A change counts only if it is more than 2 pooled standard errors AND at least 1% of the baseline.",
        "scope": "This result applies to this game or scene, these settings, and this PC only.",
    }


def render_text(rep):
    """Turn a report dictionary into the plain-English text shown to the user."""
    # First lines: the verdict and its explanation.
    lines = ["VERDICT: %s" % rep["verdict"], rep["explanation"], ""]
    lines.append("Baseline runs used: %d. Tuned runs used: %d. First %.0f seconds of every run thrown away as warm-up." % (
        len(rep["baseline_runs"]), len(rep["tuned_runs"]), rep["warmup_seconds_discarded"]))
    # Display names and units for each metric.
    names = {"avg_fps": ("Average FPS", "FPS"), "low1_fps": ("1% low FPS", "FPS"), "p99_ms": ("99th percentile frame time", "ms")}
    for key, (label, unit) in names.items():
        m = rep["metrics"].get(key)
        if m:
            # One sentence per metric with both averages, the change and the noise level.
            lines.append("%s: baseline %.2f %s, tuned %.2f %s, change %+.2f %s (%+.1f%%). Noise (2 standard errors): %.2f. Result: %s." % (
                label, m["baseline_mean"], unit, m["tuned_mean"], unit, m["difference"], unit, m["percent"], m["two_std_errors"], m["direction"]))
    # Then notes, warnings, scope and definitions.
    for note in rep["notes"]:
        lines.append("Note: " + note)
    for w in rep["warnings"]:
        lines.append("Warning: " + w)
    lines.append(rep["scope"])
    lines.append("Definitions: " + rep["definitions"])
    return "\n".join(lines) + "\n"


def save(rep):
    """Write the report as .json and .txt in the reports folder; returns both file paths."""
    # Reports go in gametune's own reports folder.
    folder = paths.sub("reports")
    os.makedirs(folder, exist_ok=True)
    # The time stamp makes the file name unique.
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.join(folder, "report-" + stamp)
    # If the name is taken (same second), add -2, -3, ...
    n = 1
    while os.path.exists(base + ".json"):
        n += 1
        base = os.path.join(folder, "report-%s-%d" % (stamp, n))
    # Write the machine-readable and the readable version side by side.
    with open(base + ".json", "w") as f:
        json.dump(rep, f, indent=2)
    with open(base + ".txt", "w") as f:
        f.write(render_text(rep))
    return base + ".json", base + ".txt"
