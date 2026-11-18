# Program: gametune - selftest.py
# Programmer: Ethan-da-Tech-Wizard (written with Claude, an AI assistant)
# Last Modified: 2026-11-18
# Purpose: Runs a short bounded headless graphics scene under gamescope with MangoHud logging, checks the thermal guard first and during the run, then parses the log to prove the measuring chain works.

"""Headless measurement check: proves the MangoHud logging and parsing chain works, with NO real game.

It runs a tiny Vulkan scene (vkmark, else vkcube) inside `gamescope --backend headless` with MangoHud logging
into a temporary folder, for a short bounded time, then parses the log. It tests the measuring tools. It does
not show that any game gets faster.
"""
# Standard library: glob finds files, signal/subprocess run and stop the scene, tempfile makes a scratch folder.
import glob
import os
import shutil
import signal
import subprocess
import tempfile
import time

# Our own helpers for temperatures, log parsing and statistics.
from . import hardware, mangohud, stats, thermal

# Install hint shown (not run) on Arch Linux when tools are missing.
INSTALL_ARCH = "sudo pacman -S --needed mangohud vkmark gamemode"
# Hard cap on the scene length in seconds.
MAX_SECONDS = 20


def _stop_group(proc):
    """Stop the process group we started (by PID, never by name)."""
    # The leader already exited.
    if proc.poll() is not None:
        # leader gone; still make sure children in the group are cleaned
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        return
    # Politely ask (SIGTERM), wait up to 3 seconds, then force stop (SIGKILL); only our own process group is signalled.
    for sig, wait in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 3.0)):
        try:
            os.killpg(proc.pid, sig)
        except (OSError, ProcessLookupError):
            return
        # Wait loop until the process ends or the wait runs out.
        end = time.time() + wait
        while time.time() < end:
            if proc.poll() is not None:
                return
            time.sleep(0.1)


def _mangohud_setup(mangohud_path, which):
    """Return (prefix_cmd, extra_env, error). mangohud_path may be a binary or an extracted tree with usr/."""
    # A path was given: it may be an extracted package tree or a program.
    if mangohud_path:
        # Where the Vulkan layer file and library would be inside an extracted tree.
        layer = os.path.join(mangohud_path, "usr", "share", "vulkan", "implicit_layer.d")
        lib = os.path.join(mangohud_path, "usr", "lib", "mangohud", "libMangoHud.so")
        # Extracted tree: use environment variables to load MangoHud, no wrapper command needed.
        if os.path.isdir(layer) and os.path.isfile(lib):
            return [], {"MANGOHUD": "1", "VK_ADD_IMPLICIT_LAYER_PATH": layer}, None
        # A plain executable file: use it as a wrapper command.
        if os.path.isfile(mangohud_path) and os.access(mangohud_path, os.X_OK):
            return [mangohud_path], {}, None
        # Neither: return an error message.
        return None, None, "--mangohud-path %s is neither an mangohud program nor an extracted tree with usr/lib/mangohud/libMangoHud.so." % mangohud_path
    # No path given: look for mangohud on PATH.
    found = which("mangohud")
    return ([found], {}, None) if found else (None, None, None)


def run(seconds=10, scene="auto", mangohud_path=None, allow_unknown_temp=False, which=shutil.which,
        sample=thermal.sample, limits=None, keep_dir=None):
    """Returns a result dict with status OK / SKIPPED / FAILED / REFUSED_HOT and plain 'lines'."""
    # Clamp the length to between 3 and MAX_SECONDS.
    seconds = max(3, min(int(seconds), MAX_SECONDS))
    # Result dictionary filled in as we go; L is a shortcut to the output lines.
    res = {"status": None, "lines": [], "missing": [], "stats": None, "command": None, "scene": None}
    L = res["lines"]
    # Work out how to run MangoHud.
    mh_prefix, mh_env, mh_err = _mangohud_setup(mangohud_path, which)
    if mh_err:
        res["status"] = "FAILED"
        L.append(mh_err)
        return res
    # Collect the names of tools that are missing.
    missing = []
    if not which("gamescope"):
        missing.append("gamescope")
    if mh_prefix is None:
        missing.append("mangohud")
    # Choose a scene: vkcube first, then vkmark (or the one asked for).
    chosen = None
    order = ["vkcube", "vkmark"] if scene == "auto" else [scene]
    for cand in order:
        if cand in ("vkcube", "vkmark") and which(cand):
            chosen = cand
            break
    if chosen is None:
        missing.append("vkmark (or vkcube)")
    if missing:
        # Missing tools mean SKIPPED (not failed); nothing runs.
        res["status"] = "SKIPPED"
        res["missing"] = missing
        L.append("SKIPPED, not failed: the self-test could not run because these tools are missing: %s." % ", ".join(missing))
        L.append("To install them on Arch Linux run: %s" % INSTALL_ARCH)
        L.append("Nothing was run and nothing was measured.")
        return res
    res["scene"] = chosen

    # Safety first: check temperatures before starting.
    s = sample()
    state, msg = thermal.evaluate(s["gpu_c"], s["cpu_c"], True, limits)
    L.append("Thermal check before start: %s" % msg)
    # --allow-unknown-temp lets the user continue when no sensor can be read.
    if state == thermal.UNKNOWN and allow_unknown_temp:
        state = thermal.OK
        L.append("Continuing because --allow-unknown-temp was given.")
    # Refuse to start unless the guard says OK.
    if state != thermal.OK:
        res["status"] = "REFUSED_HOT" if state != thermal.UNKNOWN else "REFUSED_UNKNOWN_TEMP"
        L.append("The self-test did not start. Nothing was run.")
        return res

    # Use a temporary folder (deleted afterwards unless a keep_dir was given).
    tmp = keep_dir or tempfile.mkdtemp(prefix="gametune-selftest-")
    own_tmp = keep_dir is None
    try:
        # An empty MangoHud config file so the user's own config does not interfere.
        conf = os.path.join(tmp, "empty.conf")
        open(conf, "w").close()
        logdir = os.path.join(tmp, "logs")
        os.makedirs(logdir)
        # Build the environment for the child process.
        env = dict(os.environ)
        env.update(mh_env)
        env["MANGOHUD_CONFIGFILE"] = conf
        # Tell MangoHud to log into our temp folder, start at once, and stop a few seconds before the scene ends.
        env["MANGOHUD_CONFIG"] = "output_folder=%s,autostart_log=1,log_duration=%d,log_interval=0" % (logdir, max(2, seconds - 4))
        # Different arguments for each test scene program.
        if chosen == "vkmark":
            scene_cmd = ["vkmark", "-b", "cube:duration=%d" % seconds, "-s", "640x360", "-p", "fifo"]
        else:
            scene_cmd = ["vkcube", "--c", str(seconds * 60), "--width", "640", "--height", "360"]
        # Command: gamescope headless backend, then MangoHud (if a wrapper), then the scene.
        cmd = [which("gamescope"), "--backend", "headless", "-W", "640", "-H", "360", "-w", "640", "-h", "360", "--"] + mh_prefix + scene_cmd
        res["command"] = cmd
        L.append("Running (at most %d s, hard limit %d s): %s" % (seconds + 3, MAX_SECONDS + 5, " ".join(cmd)))
        # Scene output goes to a file so we can show the tail if something fails.
        errlog = os.path.join(tmp, "stderr.txt")
        with open(errlog, "w") as ef:
            # start_new_session makes its own process group so we can stop only that group.
            proc = subprocess.Popen(cmd, env=env, stdout=ef, stderr=subprocess.STDOUT, start_new_session=True)
        L.append("Started process group %d (the only one this test will signal)." % proc.pid)
        # Monitor loop: run until the scene ends, time is up, or it gets too hot.
        start = time.time()
        stop_reason = None
        hot_msg = None
        while True:
            # Scene ended by itself.
            if proc.poll() is not None:
                stop_reason = "scene finished by itself"
                break
            # Time limit: scene length plus 3 seconds.
            if time.time() - start >= seconds + 3:
                stop_reason = "time limit reached"
                break
            # Check temperatures while running (starting=False).
            st, hot_msg = thermal.evaluate(*(lambda x: (x["gpu_c"], x["cpu_c"]))(sample()), False, limits)
            # PAUSE or ABORT: stop the scene.
            if st in (thermal.PAUSE, thermal.ABORT):
                stop_reason = "thermal guard stopped it: " + hot_msg
                res["status"] = "ABORTED_HOT"
                break
            # Check twice a second.
            time.sleep(0.5)
        # Always stop the process group.
        _stop_group(proc)
        L.append("Stopped: %s. Ran %.1f s." % (stop_reason, time.time() - start))
        if res["status"] == "ABORTED_HOT":
            return res
        # Find the frame logs (not summary files).
        logs = [p for p in glob.glob(os.path.join(logdir, "*.csv")) if not p.endswith("_summary.csv")]
        # Capture the last 600 characters of scene output for failure messages.
        tail = ""
        try:
            with open(errlog, "r", errors="replace") as ef:
                tail = ef.read()[-600:].strip()
        except OSError:
            pass
        # No log means the measuring chain is broken: report FAILED.
        if not logs:
            res["status"] = "FAILED"
            L.append("FAILED: the scene ran but MangoHud wrote no frame log. This is a real failure of the measuring chain.")
            if tail:
                L.append("Last output of the run: " + tail.replace("\n", " | "))
            return res
        # Parse the newest log.
        try:
            log = mangohud.parse_file(max(logs, key=os.path.getmtime))
        except mangohud.LogError as exc:
            res["status"] = "FAILED"
            L.append("FAILED: a log was written but could not be read: %s" % exc)
            return res
        # Compute statistics from the parsed frame times.
        m = stats.run_metrics(log.frametimes_ms)
        res["status"] = "OK"
        res["stats"] = m
        L.append("PASSED: MangoHud wrote a real frame log and it was parsed. %d frames over %.1f s." % (m["frames"], m["seconds"]))
        L.append("Average FPS %.1f, median FPS %.1f, 1%% low FPS %.1f, 0.1%% low FPS %.1f, 99th percentile frame time %.2f ms." % (
            m["avg_fps"], m["median_fps"], m["low1_fps"], m["low01_fps"], m["p99_ms"]))
        # About 60 FPS suggests the virtual screen refresh limited the scene, not the GPU.
        if m["avg_fps"] < 62 and m["avg_fps"] > 58:
            L.append("Note: about 60 FPS means the headless screen's 60 Hz refresh limited the scene, not the GPU. The FPS number shows the chain works; it is not a speed score.")
        L.append("This only shows that measuring works on this PC. It says nothing about any game or any tuning.")
        return res
    # Always clean up the scratch folder we created.
    finally:
        if own_tmp:
            shutil.rmtree(tmp, ignore_errors=True)
