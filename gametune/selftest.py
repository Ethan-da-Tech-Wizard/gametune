# ==============================================================================
# Program: selftest.py
# Programmer: Ethan-da-Tech-Wizard
# Last Modified: November 18, 2026
# Purpose: Self-test verification module ensuring measurement prerequisites are functioning.
# ==============================================================================

"""Headless measurement check: proves the MangoHud logging and parsing chain works, with NO real game.

It runs a tiny Vulkan scene (vkmark, else vkcube) inside `gamescope --backend headless` with MangoHud logging
into a temporary folder, for a short bounded time, then parses the log. It tests the measuring tools. It does
not show that any game gets faster.
"""
import glob
import os
import shutil
import signal
import subprocess
import tempfile
import time

from . import hardware, mangohud, stats, thermal

# Only printed as a hint for the user; gametune itself never runs sudo.
INSTALL_ARCH = "sudo pacman -S --needed mangohud vkmark gamemode"
# Hard cap on scene length so a self-test can never run for long.
MAX_SECONDS = 20


# Stopping by process group (we start the child with start_new_session) means
# only processes this test created are signalled, never anything matched by name.
def _stop_group(proc):
    """Stop the process group we started (by PID, never by name)."""
    if proc.poll() is not None:
        # leader gone; still make sure children in the group are cleaned
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        return
    # Escalate politely: SIGTERM, wait up to 3 s, then SIGKILL.
    for sig, wait in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 3.0)):
        try:
            os.killpg(proc.pid, sig)
        except (OSError, ProcessLookupError):
            return
        end = time.time() + wait
        while time.time() < end:
            if proc.poll() is not None:
                return
            time.sleep(0.1)


def _mangohud_setup(mangohud_path, which):
    """Return (prefix_cmd, extra_env, error). mangohud_path may be a binary or an extracted tree with usr/."""
    # Two accepted forms: an extracted package tree (use its Vulkan implicit layer via env vars) or a mangohud executable.
    if mangohud_path:
        layer = os.path.join(mangohud_path, "usr", "share", "vulkan", "implicit_layer.d")
        lib = os.path.join(mangohud_path, "usr", "lib", "mangohud", "libMangoHud.so")
        if os.path.isdir(layer) and os.path.isfile(lib):
            return [], {"MANGOHUD": "1", "VK_ADD_IMPLICIT_LAYER_PATH": layer}, None
        if os.path.isfile(mangohud_path) and os.access(mangohud_path, os.X_OK):
            return [mangohud_path], {}, None
        return None, None, "--mangohud-path %s is neither an mangohud program nor an extracted tree with usr/lib/mangohud/libMangoHud.so." % mangohud_path
    found = which("mangohud")
    return ([found], {}, None) if found else (None, None, None)


def run(seconds=10, scene="auto", mangohud_path=None, allow_unknown_temp=False, which=shutil.which,
        sample=thermal.sample, limits=None, keep_dir=None):
    """Returns a result dict with status OK / SKIPPED / FAILED / REFUSED_HOT and plain 'lines'."""
    # Clamp to 3..20 s.
    seconds = max(3, min(int(seconds), MAX_SECONDS))
    res = {"status": None, "lines": [], "missing": [], "stats": None, "command": None, "scene": None}
    # Shorthand alias for the human-readable report lines.
    L = res["lines"]
    mh_prefix, mh_env, mh_err = _mangohud_setup(mangohud_path, which)
    if mh_err:
        res["status"] = "FAILED"
        L.append(mh_err)
        return res
    # Missing tools mean SKIPPED (exit 2), not FAILED: it is an environment gap, not a bug.
    missing = []
    if not which("gamescope"):
        missing.append("gamescope")
    if mh_prefix is None:
        missing.append("mangohud")
    chosen = None
    order = ["vkcube", "vkmark"] if scene == "auto" else [scene]
    for cand in order:
        if cand in ("vkcube", "vkmark") and which(cand):
            chosen = cand
            break
    if chosen is None:
        missing.append("vkmark (or vkcube)")
    if missing:
        res["status"] = "SKIPPED"
        res["missing"] = missing
        L.append("SKIPPED, not failed: the self-test could not run because these tools are missing: %s." % ", ".join(missing))
        L.append("To install them on Arch Linux run: %s" % INSTALL_ARCH)
        L.append("Nothing was run and nothing was measured.")
        return res
    res["scene"] = chosen

    # Thermal pre-flight: refuse to start when hot or when temperatures cannot be read (unless explicitly overridden).
    s = sample()
    state, msg = thermal.evaluate(s["gpu_c"], s["cpu_c"], True, limits)
    L.append("Thermal check before start: %s" % msg)
    if state == thermal.UNKNOWN and allow_unknown_temp:
        state = thermal.OK
        L.append("Continuing because --allow-unknown-temp was given.")
    if state != thermal.OK:
        res["status"] = "REFUSED_HOT" if state != thermal.UNKNOWN else "REFUSED_UNKNOWN_TEMP"
        L.append("The self-test did not start. Nothing was run.")
        return res

    # Scratch folder is removed afterwards (see finally) unless the caller supplied keep_dir.
    tmp = keep_dir or tempfile.mkdtemp(prefix="gametune-selftest-")
    own_tmp = keep_dir is None
    try:
        # An empty MangoHud config file stops a user's own config from changing the test.
        conf = os.path.join(tmp, "empty.conf")
        open(conf, "w").close()
        logdir = os.path.join(tmp, "logs")
        os.makedirs(logdir)
        env = dict(os.environ)
        env.update(mh_env)
        env["MANGOHUD_CONFIGFILE"] = conf
        # Start logging after 1 s and stop a little before the scene ends so the log file is flushed and closed cleanly.
        env["MANGOHUD_CONFIG"] = "output_folder=%s,autostart_log=1,log_duration=%d,log_interval=0" % (logdir, max(2, seconds - 4))
        if chosen == "vkmark":
            scene_cmd = ["vkmark", "-b", "cube:duration=%d" % seconds, "-s", "640x360", "-p", "fifo"]
        else:
            scene_cmd = ["vkcube", "--c", str(seconds * 60), "--width", "640", "--height", "360"]
        # headless gamescope needs no display, so this works over SSH or in CI.
        cmd = [which("gamescope"), "--backend", "headless", "-W", "640", "-H", "360", "-w", "640", "-h", "360", "--"] + mh_prefix + scene_cmd
        res["command"] = cmd
        L.append("Running (at most %d s, hard limit %d s): %s" % (seconds + 3, MAX_SECONDS + 5, " ".join(cmd)))
        errlog = os.path.join(tmp, "stderr.txt")
        with open(errlog, "w") as ef:
            proc = subprocess.Popen(cmd, env=env, stdout=ef, stderr=subprocess.STDOUT, start_new_session=True)
        L.append("Started process group %d (the only one this test will signal)." % proc.pid)
        start = time.time()
        stop_reason = None
        hot_msg = None
        # Supervise loop: stop when the scene exits, the time limit passes, or the thermal guard trips; poll twice a second.
        while True:
            if proc.poll() is not None:
                stop_reason = "scene finished by itself"
                break
            if time.time() - start >= seconds + 3:
                stop_reason = "time limit reached"
                break
            st, hot_msg = thermal.evaluate(*(lambda x: (x["gpu_c"], x["cpu_c"]))(sample()), False, limits)
            if st in (thermal.PAUSE, thermal.ABORT):
                stop_reason = "thermal guard stopped it: " + hot_msg
                res["status"] = "ABORTED_HOT"
                break
            time.sleep(0.5)
        _stop_group(proc)
        L.append("Stopped: %s. Ran %.1f s." % (stop_reason, time.time() - start))
        if res["status"] == "ABORTED_HOT":
            return res
        # A run that produced no frame log is a real FAILED result: the measuring chain is broken.
        logs = [p for p in glob.glob(os.path.join(logdir, "*.csv")) if not p.endswith("_summary.csv")]
        tail = ""
        try:
            with open(errlog, "r", errors="replace") as ef:
                tail = ef.read()[-600:].strip()
        except OSError:
            pass
        if not logs:
            res["status"] = "FAILED"
            L.append("FAILED: the scene ran but MangoHud wrote no frame log. This is a real failure of the measuring chain.")
            if tail:
                L.append("Last output of the run: " + tail.replace("\n", " | "))
            return res
        try:
            log = mangohud.parse_file(max(logs, key=os.path.getmtime))
        except mangohud.LogError as exc:
            res["status"] = "FAILED"
            L.append("FAILED: a log was written but could not be read: %s" % exc)
            return res
        m = stats.run_metrics(log.frametimes_ms)
        res["status"] = "OK"
        res["stats"] = m
        L.append("PASSED: MangoHud wrote a real frame log and it was parsed. %d frames over %.1f s." % (m["frames"], m["seconds"]))
        L.append("Average FPS %.1f, median FPS %.1f, 1%% low FPS %.1f, 0.1%% low FPS %.1f, 99th percentile frame time %.2f ms." % (
            m["avg_fps"], m["median_fps"], m["low1_fps"], m["low01_fps"], m["p99_ms"]))
        # Headless gamescope is vsync-limited to 60 Hz; explain so users don't read it as GPU speed.
        if m["avg_fps"] < 62 and m["avg_fps"] > 58:
            L.append("Note: about 60 FPS means the headless screen's 60 Hz refresh limited the scene, not the GPU. The FPS number shows the chain works; it is not a speed score.")
        L.append("This only shows that measuring works on this PC. It says nothing about any game or any tuning.")
        return res
    # Always clean up the temp folder, even on early returns.
    finally:
        if own_tmp:
            shutil.rmtree(tmp, ignore_errors=True)
