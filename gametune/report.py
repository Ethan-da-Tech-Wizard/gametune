# ==============================================================================
# Program: report.py
# Programmer: Ethan-da-Tech-Wizard
# Last Modified: November 18, 2026
# Purpose: Comparative benchmark reporting engine evaluating baseline vs tuned frame-time distributions.
# ==============================================================================

"""Build, print and save a before/after report from real MangoHud logs."""

# File role: turns two sets of MangoHud CSV logs (baseline vs tuned) into an honest verdict. cli.py `report` calls expand -> build -> save and
# render_text. parsing is in mangohud.py, the statistics and the IMPROVED / WORSE / NO CLEAR CHANGE / NOT ENOUGH DATA decision are in stats.py.
# Design decision: refuse to conclude on thin or unfair data (too few runs, short runs, different GPU/driver, hot GPU) instead of printing a number.
# Output: a JSON file and a text file under the data folder; reports are never overwritten.

import datetime
import glob
import json
import os

from . import mangohud, paths, stats

# Minimum usable data per run (after warm-up is dropped). Shorter or sparser
# runs are skipped rather than trusted: a few seconds of frames says little
# about 1% lows.
# 1% lows are computed from the slowest 1% of frames; with fewer than ~100 frames that is one frame, so the figure would be noise.
MIN_RUN_SECONDS = 10.0
MIN_RUN_FRAMES = 100


def expand(items):
    """Files stay files; a folder becomes its *.csv frame logs (summary files skipped); sorted for stability."""

    # Order is sorted per folder so the same input always produces the same report ordering.
    out = []
    for item in items:
        if os.path.isdir(item):

            # MangoHud writes a separate *_summary.csv next to each log; it has different columns and would be parsed as a bad frame log.
            out.extend(sorted(p for p in glob.glob(os.path.join(item, "*.csv")) if not p.lower().endswith("_summary.csv")))
        else:
            out.append(item)
    return out


def load_side(files, warmup_s, min_seconds, min_frames):
    """Parse every log on one side (baseline or tuned) of the comparison.

    Returns (runs, notes, context): per-run metric dicts for logs that passed
    the size checks, human-readable notes about anything skipped, and the
    system header (gpu/cpu/driver/kernel) taken from the first good log.
    """

    # Notes explain every skipped file so the user is never left wondering why 3 logs became 2 runs.
    runs, notes = [], []
    context = {}
    for path in files:
        try:
            log = mangohud.parse_file(path)
        # An unreadable log is reported and skipped; one bad file must not abort the whole comparison.
        except mangohud.LogError as exc:
            notes.append("Skipped %s: %s" % (os.path.basename(path), exc))
            continue
        # Discard the first seconds: shader compilation and asset loading make them unrepresentative.
        # Warm-up is cut from the log's start by elapsed time, not frame count, so a slow first minute and a fast one are treated alike.
        frames = mangohud.drop_warmup(log, warmup_s)
        # Duration is the sum of frame times, which equals wall-clock time of the kept frames.
        seconds = sum(frames) / 1000.0
        if len(frames) < min_frames or seconds < min_seconds:
            notes.append("Skipped %s: only %d frames and %.1f seconds are left after throwing away the first %.0f seconds. "
                         "At least %d frames and %.0f seconds are needed." % (
                             os.path.basename(path), len(frames), seconds, warmup_s, min_frames, min_seconds))
            continue

        # One metrics dict per run; stats.compare later takes means across runs, so a single weird run does not dominate.
        m = stats.run_metrics(frames)
        m["file"] = os.path.basename(path)
        # Peak temperatures are kept so build() can warn about thermal throttling.
        m["max_gpu_temp_c"] = max(log.gpu_temps) if log.gpu_temps else None
        m["max_cpu_temp_c"] = max(log.cpu_temps) if log.cpu_temps else None
        if log.skipped_rows:
            notes.append("%s: ignored %d damaged or cut-off row(s)." % (os.path.basename(path), log.skipped_rows))
        # Only the first good log supplies the context; mismatches are checked against the other side later.
        if not context:
            context = dict(log.header)
        runs.append(m)
    return runs, notes, context


def build(baseline_files, tuned_files, warmup_s=5.0, min_runs=3, min_seconds=MIN_RUN_SECONDS, min_frames=MIN_RUN_FRAMES):
    """Compare two sets of MangoHud logs and return a JSON-serialisable report dict.

    The dict holds the verdict, the explanation, per-run metrics, comparison
    metrics, notes (skipped files) and warnings (unfair or unsafe conditions).
    """

    # Both sides go through the identical function so the comparison cannot be biased by different filtering.
    b_runs, b_notes, b_ctx = load_side(baseline_files, warmup_s, min_seconds, min_frames)
    t_runs, t_notes, t_ctx = load_side(tuned_files, warmup_s, min_seconds, min_frames)

    # min_runs is passed down so the decision to say NOT ENOUGH DATA lives in one place (stats.py).
    verdict, metrics, why = stats.compare(b_runs, t_runs, min_runs)
    warnings = []
    # A before/after test is only fair if the machine configuration stayed the same.
    # These four identify "the same machine setup"; a driver update between runs is a classic cause of false improvements.
    for key in ("gpu", "cpu", "driver", "kernel"):
        if b_ctx.get(key) and t_ctx.get(key) and b_ctx[key] != t_ctx[key]:
            warnings.append("The %s differs between baseline and tuned logs (%s vs %s). The comparison may not be fair." % (key, b_ctx[key], t_ctx[key]))
    # 86 C is where many GPUs begin to throttle; results from throttled runs are unreliable.
    hot = [r["file"] for r in b_runs + t_runs if r["max_gpu_temp_c"] is not None and r["max_gpu_temp_c"] >= 86]
    if hot:
        warnings.append("The GPU reached 86 C or more during: %s. A hot GPU slows itself down, which can distort results and is unsafe to push." % ", ".join(hot))

    # Every key here is public output (saved as JSON), so renaming one breaks anyone reading old reports.
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
    """Render a report dict (from build) as the plain-text report shown to the user."""
    lines = ["VERDICT: %s" % rep["verdict"], rep["explanation"], ""]
    lines.append("Baseline runs used: %d. Tuned runs used: %d. First %.0f seconds of every run thrown away as warm-up." % (
        len(rep["baseline_runs"]), len(rep["tuned_runs"]), rep["warmup_seconds_discarded"]))
    # Display label and unit for each metric key.
    # Order of this dict is the order lines are printed; 1% low and p99 are the figures players actually feel, so they sit next to the average.
    names = {"avg_fps": ("Average FPS", "FPS"), "low1_fps": ("1% low FPS", "FPS"), "p99_ms": ("99th percentile frame time", "ms")}
    for key, (label, unit) in names.items():
        m = rep["metrics"].get(key)
        if m:
            lines.append("%s: baseline %.2f %s, tuned %.2f %s, change %+.2f %s (%+.1f%%). Noise (2 standard errors): %.2f. Result: %s." % (
                label, m["baseline_mean"], unit, m["tuned_mean"], unit, m["difference"], unit, m["percent"], m["two_std_errors"], m["direction"]))
    for note in rep["notes"]:
        lines.append("Note: " + note)
    for w in rep["warnings"]:
        lines.append("Warning: " + w)
    lines.append(rep["scope"])
    lines.append("Definitions: " + rep["definitions"])
    return "\n".join(lines) + "\n"


def save(rep):
    """Write the report as .json and .txt under the gametune data folder.

    Returns (json_path, txt_path). Never overwrites an earlier report.
    """
    folder = paths.sub("reports")
    os.makedirs(folder, exist_ok=True)

    # Timestamp in the name keeps reports sorted by creation time in a file browser.
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.join(folder, "report-" + stamp)
    n = 1
    # Two reports in the same second get a -2, -3 suffix instead of clobbering.
    while os.path.exists(base + ".json"):
        n += 1
        base = os.path.join(folder, "report-%s-%d" % (stamp, n))

    # JSON is the machine-readable copy (for scripts); the .txt below is what a person reads.
    with open(base + ".json", "w") as f:
        json.dump(rep, f, indent=2)
    with open(base + ".txt", "w") as f:
        f.write(render_text(rep))
    return base + ".json", base + ".txt"
