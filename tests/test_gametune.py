# ==============================================================================
# File: test_gametune.py
# Purpose: Offline unit tests for gametune. They use fake /proc and /sys trees,
# synthetic MangoHud logs and injected command runners, so no real hardware,
# GPU tools or network are needed. Run with tests/run_tests.py.
# ==============================================================================
# Test strategy: nothing here touches real hardware, the network or the user's home. Every filesystem read goes through a temporary fake
# /proc and /sys tree, every external command is replaced by an injected `runner`/`which`, and every write lands under a temp GAMETUNE_HOME.
# Classes map to modules: ParserTests -> mangohud.py, StatsTests -> stats.py, ReportTests -> report.py, ThermalTests -> thermal.py and selftest.py,
# HardwareTests -> hardware.py, ProfileTests -> profiles.py and cli.py. Pinned contracts are the ones users rely on: honest verdicts and exact undo.

import os
import sys
import tempfile
import unittest

# Make the repo root importable so `import gametune` works without installing.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gametune import hardware, mangohud, paths, profiles, report, selftest, stats, thermal  # noqa: E402

# Fixture: the two-line system header MangoHud writes at the top of a log.
# Real MangoHud writes this pair of lines first; the parser must locate the column line after them (line 3).
HEADER = "os,cpu,gpu,ram,kernel,driver,cpuscheduler\nLinux,TestCPU,TestGPU,16000,6.1,drv1,powersave\n"
# Fixture: the column-name line; `elapsed` is in nanoseconds.
COLS = "fps,frametime,cpu_load,gpu_temp,cpu_temp,elapsed\n"


def log_text(frametimes, gpu_temp=60, header=True, cols=COLS):
    """Build MangoHud-format CSV text from a list of frame times in milliseconds."""

    # Running total of nanoseconds so each row's `elapsed` is cumulative, like a real log.
    t = 0.0
    rows = []
    for ft in frametimes:
        # ms -> ns, matching MangoHud's `elapsed` column.
        t += ft * 1e6
        rows.append("%.2f,%.4f,10,%s,50,%d" % (1000.0 / ft, ft, gpu_temp, t))
    return (HEADER if header else "") + cols + "\n".join(rows) + "\n"


def steady(fps, seconds=30, jitter=0.0):
    """Frame times for a steady `fps` run; jitter alternates +/- that fraction to add noise."""

    # Number of frames for the requested duration; constant frame times make expected results easy to compute by hand.
    n = int(fps * seconds)
    base = 1000.0 / fps
    return [base * (1 + jitter * (1 if i % 2 else -1)) for i in range(n)]


class Tmp(unittest.TestCase):
    """Base class: gives each test a temp dir and points GAMETUNE_HOME inside it so nothing real is touched."""
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = d.name

        # Saved and restored so tests cannot leak the temp path into each other or into the developer's shell.
        old = os.environ.get("GAMETUNE_HOME")
        os.environ["GAMETUNE_HOME"] = os.path.join(self.dir, "home")
        self.addCleanup(lambda: os.environ.__setitem__("GAMETUNE_HOME", old) if old else os.environ.pop("GAMETUNE_HOME", None))

    def write(self, name, text):
        """Write a file in the temp dir and return its path."""
        p = os.path.join(self.dir, name)
        with open(p, "w") as f:
            f.write(text)
        return p


class ParserTests(Tmp):
    """MangoHud log parsing: header variants, missing columns, damaged input."""
    def test_full_log(self):
        """A complete log parses; header fields and elapsed time are read."""
        log = mangohud.parse_text(log_text([16.0] * 10))
        # Ten rows in, ten frames out: nothing is dropped from a clean log.
        self.assertEqual(len(log.frametimes_ms), 10)
        self.assertEqual(log.header["gpu"], "TestGPU")
        # 10 frames x 16 ms = 0.16 s on the elapsed clock.
        self.assertAlmostEqual(log.times_s[-1], 0.16, places=3)

    def test_no_system_header(self):
        """Older logs without the system header still parse, with an empty header dict."""
        log = mangohud.parse_text(log_text([20.0] * 5, header=False))
        self.assertEqual(len(log.frametimes_ms), 5)
        # No header lines means an empty dict, not an error.
        self.assertEqual(log.header, {})

    def test_missing_columns_fps_only(self):
        """With no frametime column, frame times are derived from fps (1000/fps)."""
        log = mangohud.parse_text("fps,gpu_temp\n50,60\n100,61\n")
        # 50 FPS -> 20 ms and 100 FPS -> 10 ms.
        self.assertEqual([round(x) for x in log.frametimes_ms], [20, 10])

    def test_no_elapsed_uses_cumulative(self):
        """Without an elapsed column the clock is the running sum of frame times."""
        log = mangohud.parse_text("frametime\n10\n10\n10\n")
        self.assertAlmostEqual(log.times_s[-1], 0.03, places=6)

    def test_truncated_last_row_skipped(self):
        """A cut-off final row is counted as skipped, not fatal."""
        # Append a row with too few fields, as if the game was killed mid-write.
        text = log_text([16.0] * 4) + "60.0,16.0,1\n"
        log = mangohud.parse_text(text)
        self.assertEqual(len(log.frametimes_ms), 4)
        self.assertEqual(log.skipped_rows, 1)

    def test_garbage_and_empty(self):
        """Empty, non-CSV, and header-only inputs raise LogError."""
        # Three bad inputs: empty, not a MangoHud file, and a column line with no data rows.
        for bad in ("", "hello\nworld\n", COLS):
            with self.assertRaises(mangohud.LogError):
                mangohud.parse_text(bad)

    def test_summary_file_refused(self):
        """*_summary.csv files are rejected because they hold no frame data."""
        # The name alone must trigger the refusal, whatever the contents.
        p = self.write("run_summary.csv", "0.1% Percentile,1% Percentile\n1,2\n")
        with self.assertRaises(mangohud.LogError):
            mangohud.parse_file(p)

    def test_warmup_dropped(self):
        """drop_warmup removes the first N seconds, and everything if N exceeds the run."""
        log = mangohud.parse_text(log_text([100.0] * 100))   # 10 s at 100 ms
        # Half of a 10 s log should remain after discarding 5 s.
        kept = mangohud.drop_warmup(log, 5.0)
        self.assertAlmostEqual(sum(kept) / 1000.0, 5.0, delta=0.2)
        self.assertEqual(mangohud.drop_warmup(log, 50.0), [])

    def test_drop_warmup_returns_one_plain_list(self):
        """drop_warmup returns a single list of frametimes, never a (frametimes, temps) tuple.

        The docstring used to promise a tuple; callers (cli/report) index the result as a list.
        This pins the real contract, including the warmup_s <= 0 branch.
        """
        log = mangohud.parse_text(log_text([100.0] * 100))
        for warmup in (0, 5.0, 50.0):
            self.assertIsInstance(mangohud.drop_warmup(log, warmup), list)
        self.assertEqual(len(mangohud.drop_warmup(log, 0)), 100)


class StatsTests(unittest.TestCase):
    """Metric definitions and the noise-aware verdict rules."""
    def test_metrics(self):
        """Median FPS, 1% low (worst single frame of 100) and average FPS match hand calculation."""
        # 98 fast frames plus two stutters: the median ignores them, the 1% low must not.
        ft = [10.0] * 98 + [50.0, 100.0]
        m = stats.run_metrics(ft)

        # 98 of 100 frames are 10 ms, so the median is exactly 10 ms = 100 FPS regardless of the two stutters.
        self.assertAlmostEqual(m["median_fps"], 100.0)
        self.assertAlmostEqual(m["low1_fps"], 1000.0 / 100.0)   # worst 1 frame of 100
        self.assertAlmostEqual(m["avg_fps"], 100.0 / (sum(ft) / 1000.0) / 1.0 * 1.0, places=6)

    # `shift` makes each run a little faster than the previous one, which creates run-to-run spread without randomness (tests stay deterministic).
    def run_set(self, fps, n, jitter=0.0, shift=0.0):
        """Helper: n synthetic run-metric dicts around `fps`."""
        return [stats.run_metrics(steady(fps + shift * i, 20, jitter)) for i in range(n)]

    def test_identical_is_no_change(self):
        """Comparing a set of runs with itself must never report a change."""
        # Three identical runs on both sides: zero difference, so any claimed change would be a false positive.
        # `list(a)` below is a copy so the same data is compared on both sides without sharing the same list object.
        a = self.run_set(60, 3)
        self.assertEqual(stats.compare(a, list(a))[0], stats.NO_CHANGE)

    def test_clear_improvement(self):
        """A ~33% FPS gain well beyond the noise is IMPROVED."""
        # Baseline ~60 FPS with small run-to-run spread; tuned ~80 FPS below.
        # The 0.1 jitter makes frame times alternate so variance inside a run is non-zero, like real gameplay.
        base = [stats.run_metrics(steady(f, 20, 0.1)) for f in (60, 61, 59)]
        tuned = [stats.run_metrics(steady(f, 20, 0.1)) for f in (80, 81, 79)]
        self.assertEqual(stats.compare(base, tuned)[0], stats.IMPROVED)

    def test_clear_worse(self):
        """A large FPS drop is reported as WORSE."""
        base = [stats.run_metrics(steady(f, 20)) for f in (80, 81, 79)]
        tuned = [stats.run_metrics(steady(f, 20)) for f in (60, 61, 59)]
        self.assertEqual(stats.compare(base, tuned)[0], stats.WORSE)

    def test_noisy_small_difference_is_no_change(self):
        """A +2 FPS shift buried in +/-10 FPS run-to-run spread must not count as a win."""
        # Huge spread between runs (50-70 FPS) means a 2 FPS shift is indistinguishable from noise.
        base = [stats.run_metrics(steady(f, 20)) for f in (50, 70, 60)]
        tuned = [stats.run_metrics(steady(f, 20)) for f in (52, 72, 62)]
        self.assertEqual(stats.compare(base, tuned)[0], stats.NO_CHANGE)

    def test_too_few_runs(self):
        """Two runs per side give NOT ENOUGH DATA, and the 3-run minimum cannot be lowered by the caller."""

        # Two runs is exactly one short of the minimum, the case most likely to tempt a lazy implementation into a verdict.
        a = self.run_set(60, 2)
        v = stats.compare(a, a)
        self.assertEqual(v[0], stats.NOT_ENOUGH)
        self.assertEqual(stats.compare(a, a, min_runs=1)[0], stats.NOT_ENOUGH)   # minimum is 3, cannot be lowered

    def test_under_one_percent_ignored(self):
        """A perfectly consistent 0.5% change is below the practical floor, so no change."""
        base = [stats.run_metrics(steady(f, 20)) for f in (60.0, 60.0, 60.0)]
        # 0.5% better but perfectly consistent: statistically clear yet too small to matter.
        tuned = [stats.run_metrics(steady(f, 20)) for f in (60.3, 60.3, 60.3)]
        self.assertEqual(stats.compare(base, tuned)[0], stats.NO_CHANGE)


class ReportTests(Tmp):
    """End-to-end report building from log files on disk."""
    def make(self, prefix, fps_list, seconds=20):
        """Helper: write one synthetic log per entry of fps_list and return the paths."""
        out = []
        for i, f in enumerate(fps_list):
            out.append(self.write("%s%d.csv" % (prefix, i), log_text(steady(f, seconds))))
        return out

    def test_report_improved_and_saved(self):
        """A clear gain gives IMPROVED and the report is saved under GAMETUNE_HOME."""

        # 60 vs 90 FPS is a 50% gain, far outside the roughly 1 FPS spread between runs, so the verdict must be IMPROVED.
        b = self.make("b", (60, 61, 59))
        t = self.make("t", (90, 91, 89))
        # warmup_s=2 keeps the 20 s synthetic runs long enough after trimming.
        rep = report.build(b, t, warmup_s=2)
        self.assertEqual(rep["verdict"], "IMPROVED")
        j, txt = report.save(rep)
        self.assertTrue(j.startswith(os.environ["GAMETUNE_HOME"]) and os.path.isfile(txt))

    def test_two_runs_not_enough(self):
        """Fewer than 3 runs per side must not produce a verdict."""

        # Only two logs per side; the report must refuse to claim anything.
        b = self.make("b", (60, 61))
        t = self.make("t", (90, 91))
        self.assertEqual(report.build(b, t, warmup_s=2)["verdict"], "NOT ENOUGH DATA")

    def test_short_runs_excluded(self):
        """Runs too short after warm-up are dropped with a note, leaving too little data."""
        # 4 s runs fall under the 10 s minimum once warm-up is removed.
        b = self.make("b", (60, 61, 59), seconds=4)
        t = self.make("t", (90, 91, 89), seconds=4)
        rep = report.build(b, t, warmup_s=2)
        self.assertEqual(rep["verdict"], "NOT ENOUGH DATA")
        self.assertTrue(rep["notes"])

    def test_hot_warning(self):
        """Logs showing an 88 C GPU make the report carry a thermal warning."""

        # 88 C is above the 86 C threshold in report.build, which proves the warning is driven by the log's own temperature column.
        b = [self.write("b%d.csv" % i, log_text(steady(60, 20), gpu_temp=88)) for i in range(3)]
        t = self.make("t", (60, 60, 60))
        self.assertTrue(report.build(b, t, warmup_s=2)["warnings"])


class ThermalTests(unittest.TestCase):
    """Thermal guard thresholds, limit tightening, selftest gating and sensor reading."""
    def test_thresholds(self):
        """Boundary values for OK / TOO_HOT_TO_START / PAUSE / ABORT / UNKNOWN on both GPU and CPU."""
        # Short alias to keep the table of boundary checks readable.
        E = thermal.evaluate
        self.assertEqual(E(84, 70, True)[0], thermal.OK)
        self.assertEqual(E(85, 70, True)[0], thermal.TOO_HOT_TO_START)
        # 85 C is too hot to START heavy work but fine for work already running.
        self.assertEqual(E(85, 70, False)[0], thermal.OK)

        # Boundaries: 86 pauses, 91 aborts. Pinning exact limits stops accidental loosening of a safety margin.
        self.assertEqual(E(86, 70, False)[0], thermal.PAUSE)
        self.assertEqual(E(91, 70, False)[0], thermal.ABORT)
        self.assertEqual(E(60, 94, False)[0], thermal.PAUSE)
        self.assertEqual(E(60, 96, False)[0], thermal.ABORT)
        self.assertEqual(E(None, None, True)[0], thermal.UNKNOWN)
        # One readable sensor is enough; only both missing is UNKNOWN.
        self.assertEqual(E(70, None, True)[0], thermal.OK)

    def test_limits_only_stricter(self):
        """Looser limits are clamped to defaults; stricter ones are honoured."""
        # Callers asking for laxer limits than the defaults must be ignored.
        # 99 and 120 are deliberately absurd to prove clamping, not tuned values.
        lim = thermal.Limits(gpu_start=99, gpu_pause=99, gpu_abort=99, cpu_abort=120)
        self.assertEqual((lim.gpu_start, lim.gpu_pause, lim.gpu_abort, lim.cpu_abort), (84, 86, 91, 96))
        self.assertEqual(thermal.evaluate(80, 50, True, thermal.Limits(gpu_start=70))[0], thermal.TOO_HOT_TO_START)

    def test_never_97(self):
        """97 C on the CPU always aborts, the hard safety ceiling."""
        self.assertEqual(thermal.evaluate(50, 97, False)[0], thermal.ABORT)

    def test_selftest_refuses_hot(self):
        """A hot GPU stops the self-test before it starts anything."""
        # Pretend every tool is installed so only the thermal check can stop the run.
        # Fake "installed" paths so selftest gets past the missing-tool check; no process is ever started because the thermal guard refuses first.
        which = lambda n: "/bin/" + n
        r = selftest.run(which=which, sample=lambda: {"gpu_c": 88.0, "cpu_c": 50.0}, mangohud_path=None)
        self.assertEqual(r["status"], "REFUSED_HOT")

    def test_selftest_skips_when_missing(self):
        """Missing tools give a SKIPPED (not FAILED) result with install advice."""
        # Pretend no tool is installed.
        # which always returns None -> every tool missing, which must be SKIPPED (exit 2) rather than FAILED (exit 1).
        r = selftest.run(which=lambda n: None, sample=lambda: {"gpu_c": 50.0, "cpu_c": 50.0})
        self.assertEqual(r["status"], "SKIPPED")
        text = " ".join(r["lines"])
        self.assertIn("SKIPPED, not failed", text)
        self.assertIn("sudo pacman -S --needed mangohud vkmark gamemode", text)
        self.assertIn("mangohud", r["missing"])

    def test_sensor_reading_fake_tree(self):
        """The CPU reading prefers the "Package id 0" sensor (71 C) over a hotter core (80 C)."""
        with tempfile.TemporaryDirectory() as root:
            # Build a fake hwmon node shaped like an Intel coretemp driver.
            # Same layout as /sys/class/hwmon on Intel: a "name" file plus tempN_input (millidegrees) and tempN_label files.
            hw = os.path.join(root, "sys/class/hwmon/hwmon0")
            os.makedirs(hw)
            open(os.path.join(hw, "name"), "w").write("coretemp\n")
            open(os.path.join(hw, "temp1_input"), "w").write("71000\n")
            open(os.path.join(hw, "temp1_label"), "w").write("Package id 0\n")
            # A hotter core with no Package label; the package value must still win.
            open(os.path.join(hw, "temp2_input"), "w").write("80000\n")
            self.assertEqual(thermal.read_cpu_temp(root), 71.0)
            # No nvidia-smi and no DRM tree: unknown, not zero.
            self.assertIsNone(thermal.read_gpu_temp(root, runner=lambda a: None))


class HardwareTests(unittest.TestCase):
    """Hardware detection against fake /proc and /sys trees."""
    def build(self, root, hybrid=True):
        """Create a fake system: 4 threads on 2 cores, 8 GiB RAM, an AMD and an Intel GPU, optionally hybrid CPU layout."""
        def w(rel, text):
            p = os.path.join(root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").write(text)
        # Four logical CPUs; each topology core_id below pairs them into two physical cores.
        w("proc/cpuinfo", "".join("processor\t: %d\nmodel name\t: Fake CPU 9000\n\n" % i for i in range(4)))
        w("proc/meminfo", "MemTotal:       8388608 kB\n")
        for i in range(4):
            w("sys/devices/system/cpu/cpu%d/topology/core_id" % (i // 2), str(i // 2))
            w("sys/devices/system/cpu/cpu%d/topology/physical_package_id" % i, "0")
        if hybrid:
            w("sys/devices/cpu_core/cpus", "0-1")
            w("sys/devices/cpu_atom/cpus", "2-3")

        # Vendor ids mirror real sysfs values: 0x1002 AMD, 0x8086 Intel.
        w("sys/class/drm/card0/device/vendor", "0x1002\n")
        w("sys/class/drm/card0/device/device", "0x73bf\n")
        # 8 GiB in bytes, the unit sysfs uses.
        w("sys/class/drm/card0/device/mem_info_vram_total", str(8 * 1024 ** 3))
        w("sys/class/drm/card1/device/vendor", "0x8086\n")
        w("sys/class/drm/card1/device/device", "0x46a6\n")

    def test_fake_tree(self):
        """A full scan of the fake tree reports every field correctly."""
        # Canned `lspci -mm -nn` output so no real lspci is run.
        # Second card is listed first in lspci on purpose; names are matched by PCI id, not by order.
        lspci = '00:02.0 "VGA compatible controller [0300]" "Intel Corporation [8086]" "Fake iGPU [46a6]" -r0c\n' \
                '03:00.0 "VGA compatible controller [0300]" "AMD [1002]" "Fake Radeon [73bf]" -r0c\n'
        with tempfile.TemporaryDirectory() as root:
            self.build(root)
            info = hardware.scan(root=root, runner=lambda a: lspci if a[0] == "lspci" else None,
                                 environ={"XDG_SESSION_TYPE": "Wayland"}, which=lambda n: "/x/" + n if n == "gamescope" else None)
        self.assertEqual(info["cpu"]["model"], "Fake CPU 9000")
        self.assertEqual(info["cpu"]["logical_cpus"], 4)

        # 4 threads with core_id 0,0,1,1 -> 2 physical cores, proving SMT siblings are not double-counted.
        self.assertEqual(info["cpu"]["physical_cores"], 2)
        self.assertTrue(info["cpu"]["hybrid"])
        self.assertEqual((info["cpu"]["performance_cpus"], info["cpu"]["efficiency_cpus"]), ("0-1", "2-3"))
        self.assertEqual(info["ram"]["total_mib"], 8192)
        self.assertEqual(info["session"]["type"], "wayland")
        amd = info["gpus"][0]
        self.assertEqual((amd["vendor"], amd["vram_mib"], amd["name"]), ("AMD", 8192, "AMD Fake Radeon"))
        self.assertTrue(info["gpus"][1]["kind"].startswith("integrated"))
        self.assertEqual(info["helpers"]["gamescope"], "/x/gamescope")
        self.assertIsNone(info["helpers"]["mangohud"])
        self.assertFalse(info["thermal_sensors"]["gpu_readable"])
        self.assertEqual(hardware.primary_gpu_vendor(info), "AMD")

    def test_non_hybrid(self):
        """Without cpu_core/cpu_atom nodes the CPU is not flagged hybrid."""
        with tempfile.TemporaryDirectory() as root:
            self.build(root, hybrid=False)
            self.assertFalse(hardware.detect_cpu(root)["hybrid"])

    def test_nvidia_smi_overrides(self):
        """nvidia-smi output supplies name and VRAM even with no sysfs data."""
        # Canned nvidia-smi CSV: name, memory in MiB, driver version.
        # Root is a nonexistent path so sysfs finds nothing; the GPU can only come from nvidia-smi, which isolates that code path.
        smi = "Fake RTX, 6144, 999.1\n"
        gpus = hardware.detect_gpus(root="/nonexistent", runner=lambda a: smi if a[0] == "nvidia-smi" else None)
        self.assertEqual((gpus[0]["vendor"], gpus[0]["vram_mib"], gpus[0]["kind"]), ("NVIDIA", 6144, "discrete"))

    def test_cpulist(self):
        """CPU-list parse/format round trip and bad-input handling."""
        self.assertEqual(hardware.parse_cpulist("0-2,5"), [0, 1, 2, 5])
        self.assertEqual(hardware.format_cpulist([0, 1, 2, 5]), "0-2,5")

        # Bad input must give an empty list rather than a partial one, since a partial list could pin a game to the wrong cores.
        self.assertEqual(hardware.parse_cpulist("junk"), [])

    def test_empty_root_does_not_crash(self):
        """An empty filesystem yields empty results rather than exceptions."""
        with tempfile.TemporaryDirectory() as root:
            info = hardware.scan(root=root, runner=lambda a: None, environ={}, which=lambda n: None)
            self.assertEqual(info["gpus"], [])
            self.assertEqual(info["session"]["type"], "unknown")


class ProfileTests(Tmp):
    """Planning, applying and reverting profiles, including all safety guards."""
    def tree(self):
        """Snapshot of every file (path -> bytes) under the temp dir, for before/after comparisons."""
        out = {}
        for d, _s, fs in os.walk(self.dir):
            for f in fs:
                p = os.path.join(d, f)
                out[p] = open(p, "rb").read()
        return out

    def test_dry_run_writes_nothing(self):
        """plan() is pure: the disk, including the data folder, is untouched."""
        # Snapshot first so we can prove nothing changed.
        # Whole-tree snapshot is the strongest check that "dry run" really writes nothing, including no empty folders.
        before = self.tree()
        pl = profiles.plan("mygame", add=("fps-cap", {"fps": "60"}))
        self.assertTrue(pl["files"])
        self.assertEqual(self.tree(), before)
        self.assertFalse(os.path.exists(paths.home()))

    def test_cli_dry_run_and_yes(self):
        """The CLI writes nothing without --yes, writes with it, and `revert --all` removes it again."""
        from gametune import cli
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["apply", "fps-cap", "--game", "g1"]), 0)
        self.assertIn("Dry run", buf.getvalue())
        self.assertFalse(os.path.exists(paths.home()))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["apply", "fps-cap", "--game", "g1", "--yes"]), 0)
        self.assertTrue(os.path.isfile(paths.sub("launch", "g1.launch.txt")))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["revert", "--all"]), 0)
        self.assertFalse(os.path.exists(paths.sub("launch", "g1.launch.txt")))

    def test_apply_revert_round_trip_restores_bytes(self):
        """After two applies, revert restores a pre-existing file byte-for-byte (including non-UTF-8 bytes)."""
        # Pre-create a file gametune would overwrite, with bytes that are not valid UTF-8, to prove exact restoration.
        # Non-UTF-8 bytes (\xff\x00) prove the journal stores raw bytes (base64) rather than text that could be altered by decoding.
        launch = paths.sub("launch", "g2.launch.txt")
        os.makedirs(os.path.dirname(launch))
        with open(launch, "wb") as f:
            f.write(b"user's own text \xff\x00\n")
        before = self.tree()
        for name, params in (("fps-cap", {"fps": "75"}), ("render-scale", {"scale": "0.6", "output": "2560x1440"})):
            profiles.apply_plan("g2", profiles.plan("g2", add=(name, params)), "apply " + name)
        text = open(launch).read()
        self.assertIn("fps_limit=75", text)
        # 2560x1440 x 0.6 = 1536x864.
        self.assertIn("gamescope -w 1536 -h 864 -W 2560 -H 1440 -F fsr", text)
        self.assertTrue(os.access(paths.sub("wrappers", "g2.sh"), os.X_OK))
        ok, msg = profiles.revert_game("g2")
        self.assertTrue(ok, msg)
        self.assertEqual(self.tree(), before)

    def test_revert_single_profile_rebuilds(self):
        """Removing one of two profiles rebuilds the launch line; removing the last leaves no files."""
        for name, params in (("fps-cap", {"fps": "75"}), ("shader-cache", {})):

            # shader-cache needs a vendor to choose between NVIDIA and Mesa variables, hence the hw dict.
            profiles.apply_plan("g3", profiles.plan("g3", add=(name, params), hw={"gpu_vendor": "NVIDIA"}), "a")
        # Reverting one of two profiles must rebuild the launch line from what remains.
        ok, _ = profiles.revert_profile("g3", "fps-cap")
        self.assertTrue(ok)
        text = open(paths.sub("launch", "g3.launch.txt")).read()
        self.assertNotIn("fps_limit", text)
        self.assertIn("__GL_SHADER_DISK_CACHE_SKIP_CLEANUP=1", text)
        ok, _ = profiles.revert_profile("g3", "shader-cache")
        self.assertTrue(ok)
        self.assertEqual(self.tree(), {})

    def test_revert_refuses_if_user_edited(self):
        """A file edited after gametune wrote it is not overwritten unless --force/force=True."""
        profiles.apply_plan("g4", profiles.plan("g4", add=("fps-cap", {})), "a")
        with open(paths.sub("launch", "g4.launch.txt"), "a") as f:
            # Simulate the user hand-editing the file gametune wrote.
            f.write("edited")

        # Refusal must come with an actionable message mentioning --force, so the user knows how to override on purpose.
        ok, msg = profiles.revert_game("g4")
        self.assertFalse(ok)
        self.assertIn("--force", msg)
        self.assertTrue(profiles.revert_game("g4", force=True)[0])

    def test_unproven_hidden_and_guarded(self):
        """Unproven profiles are hidden by default and refused without --experimental."""
        names = [p.name for p in profiles.list_profiles()]
        self.assertNotIn("dxvk-async", names)
        self.assertNotIn("core-pinning", names)
        self.assertIn("dxvk-async", [p.name for p in profiles.list_profiles(True)])
        from gametune import cli
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["apply", "dxvk-async", "--game", "g5", "--yes"]), 1)
        self.assertFalse(os.path.exists(paths.home()))

    def test_core_pinning_uses_detected_cores(self):
        """taskset uses the detected performance cores, and is refused when none are known."""
        pl = profiles.plan("g6", add=("core-pinning", {}), hw={"performance_cpus": "0-3"})
        self.assertIn("taskset -c 0-3", list(pl["files"].values())[0][0].decode())
        with self.assertRaises(profiles.ProfileError):
            profiles.plan("g6", add=("core-pinning", {}), hw={})

    def test_bad_names_and_params(self):
        """Path-like game names and out-of-range or non-numeric options raise ProfileError."""

        # Each of these would escape the data folder or confuse a shell: traversal, subdirectory, empty, leading dash, space.
        for bad in ("../x", "a/b", "", "-x", "a b"):
            with self.assertRaises(profiles.ProfileError):
                profiles.check_game(bad)
        with self.assertRaises(profiles.ProfileError):
            profiles.plan("g7", add=("fps-cap", {"fps": "abc"}))
        with self.assertRaises(profiles.ProfileError):
            profiles.plan("g7", add=("render-scale", {"scale": "0.1"}))

    def test_no_sudo_or_network_in_source(self):
        """Guard rail: the package source must not contain network, sudo or process-killing code."""
        src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # Concatenate all package source so the banned words can be searched in one place.
        blob = ""
        for f in os.listdir(os.path.join(src, "gametune")):
            if f.endswith(".py"):
                blob += open(os.path.join(src, "gametune", f)).read()

        # Static check on source text: offline operation and "never kills programs" are promises of the project, so a banned import fails the build.
        for word in ("import socket", "urllib", "http.client", "requests", "pkill", "killall", '"sudo"'):
            self.assertNotIn(word, blob, word)


if __name__ == "__main__":
    unittest.main()
