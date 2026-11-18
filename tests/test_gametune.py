# Program: gametune - test_gametune.py
# Programmer: Ethan-da-Tech-Wizard (written with Claude, an AI assistant)
# Last Modified: 2026-11-18
# Purpose: Unit tests for gametune: log parsing, statistics, reports, thermal guard, self-test, hardware detection on fake file trees, and profile apply/revert behaviour.
"""Unit tests for gametune. Every test uses fake data or a temporary folder, never the real PC state."""
# Standard library modules used by the tests.
import os
import sys
import tempfile
import unittest

# Make the project folder importable no matter where the tests are started.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The modules under test.
from gametune import hardware, mangohud, paths, profiles, report, selftest, stats, thermal  # noqa: E402

# Sample MangoHud system header (two lines: names, then values).
HEADER = "os,cpu,gpu,ram,kernel,driver,cpuscheduler\nLinux,TestCPU,TestGPU,16000,6.1,drv1,powersave\n"
# Sample column-name line of a frame log.
COLS = "fps,frametime,cpu_load,gpu_temp,cpu_temp,elapsed\n"


def log_text(frametimes, gpu_temp=60, header=True, cols=COLS):
    """Build the text of a fake MangoHud log from a list of frame times in milliseconds."""
    # t is the elapsed time in nanoseconds; each row is fps, frametime, cpu load, gpu temp, cpu temp, elapsed.
    t = 0.0
    rows = []
    for ft in frametimes:
        t += ft * 1e6
        rows.append("%.2f,%.4f,10,%s,50,%d" % (1000.0 / ft, ft, gpu_temp, t))
    return (HEADER if header else "") + cols + "\n".join(rows) + "\n"


def steady(fps, seconds=30, jitter=0.0):
    """Return frame times (ms) for a steady frame rate; jitter makes alternate frames longer or shorter."""
    # Number of frames = fps * seconds; each frame lasts 1000/fps ms, nudged by the jitter.
    n = int(fps * seconds)
    base = 1000.0 / fps
    return [base * (1 + jitter * (1 if i % 2 else -1)) for i in range(n)]


class Tmp(unittest.TestCase):
    """Base class giving each test a temporary folder and a private GAMETUNE_HOME."""
    def setUp(self):
        """Create the temporary folder and point GAMETUNE_HOME at it."""
        # TemporaryDirectory is deleted automatically after the test; addCleanup registers that.
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = d.name
        # Remember the old setting so cleanup can put it back.
        old = os.environ.get("GAMETUNE_HOME")
        # Point gametune's data folder inside the temporary folder.
        os.environ["GAMETUNE_HOME"] = os.path.join(self.dir, "home")
        # Cleanup puts GAMETUNE_HOME back, or removes it if it was unset.
        self.addCleanup(lambda: os.environ.__setitem__("GAMETUNE_HOME", old) if old else os.environ.pop("GAMETUNE_HOME", None))

    def write(self, name, text):
        """Write text to a file in the temporary folder and return its path."""
        p = os.path.join(self.dir, name)
        with open(p, "w") as f:
            f.write(text)
        return p


class ParserTests(Tmp):
    """Tests for the MangoHud log parser."""
    def test_full_log(self):
        """A full log with header parses correctly."""
        # Ten frames of 16 ms: the parser should keep all of them and read the header.
        log = mangohud.parse_text(log_text([16.0] * 10))
        self.assertEqual(len(log.frametimes_ms), 10)
        self.assertEqual(log.header["gpu"], "TestGPU")
        self.assertAlmostEqual(log.times_s[-1], 0.16, places=3)

    def test_no_system_header(self):
        """A log without the system header parses with an empty header."""
        log = mangohud.parse_text(log_text([20.0] * 5, header=False))
        self.assertEqual(len(log.frametimes_ms), 5)
        self.assertEqual(log.header, {})

    def test_missing_columns_fps_only(self):
        """A log with only fps and gpu_temp columns parses (frame time is derived from fps)."""
        log = mangohud.parse_text("fps,gpu_temp\n50,60\n100,61\n")
        self.assertEqual([round(x) for x in log.frametimes_ms], [20, 10])

    def test_no_elapsed_uses_cumulative(self):
        """Without an elapsed column, cumulative frame time is used for timestamps."""
        log = mangohud.parse_text("frametime\n10\n10\n10\n")
        self.assertAlmostEqual(log.times_s[-1], 0.03, places=6)

    def test_truncated_last_row_skipped(self):
        """A cut-off last row is skipped and counted."""
        text = log_text([16.0] * 4) + "60.0,16.0,1\n"
        log = mangohud.parse_text(text)
        self.assertEqual(len(log.frametimes_ms), 4)
        self.assertEqual(log.skipped_rows, 1)

    def test_garbage_and_empty(self):
        """Empty text, nonsense and a column line with no rows raise LogError."""
        for bad in ("", "hello\nworld\n", COLS):
            with self.assertRaises(mangohud.LogError):
                mangohud.parse_text(bad)

    def test_summary_file_refused(self):
        """A MangoHud summary file is refused."""
        p = self.write("run_summary.csv", "0.1% Percentile,1% Percentile\n1,2\n")
        with self.assertRaises(mangohud.LogError):
            mangohud.parse_file(p)

    def test_warmup_dropped(self):
        """Warm-up seconds are dropped from the start of a log."""
        log = mangohud.parse_text(log_text([100.0] * 100))   # 10 s at 100 ms
        kept = mangohud.drop_warmup(log, 5.0)
        self.assertAlmostEqual(sum(kept) / 1000.0, 5.0, delta=0.2)
        self.assertEqual(mangohud.drop_warmup(log, 50.0), [])


class StatsTests(unittest.TestCase):
    """Tests for the statistics and verdict rules."""
    def test_metrics(self):
        """Average, median, 1% low and p99 are computed correctly."""
        # 98 fast frames plus one slow and one very slow frame.
        ft = [10.0] * 98 + [50.0, 100.0]
        m = stats.run_metrics(ft)
        self.assertAlmostEqual(m["median_fps"], 100.0)
        # The slowest 1% of 100 frames is the single 100 ms frame, i.e. 10 fps.
        self.assertAlmostEqual(m["low1_fps"], 1000.0 / 100.0)   # worst 1 frame of 100
        self.assertAlmostEqual(m["avg_fps"], 100.0 / (sum(ft) / 1000.0) / 1.0 * 1.0, places=6)

    def run_set(self, fps, n, jitter=0.0, shift=0.0):
        """Build n per-run metric dictionaries at the given fps."""
        return [stats.run_metrics(steady(fps + shift * i, 20, jitter)) for i in range(n)]

    def test_identical_is_no_change(self):
        """Identical sides give NO CHANGE."""
        a = self.run_set(60, 3)
        self.assertEqual(stats.compare(a, list(a))[0], stats.NO_CHANGE)

    def test_clear_improvement(self):
        """A big gain is IMPROVED."""
        # 60 fps versus 80 fps with small jitter is a clear improvement.
        base = [stats.run_metrics(steady(f, 20, 0.1)) for f in (60, 61, 59)]
        tuned = [stats.run_metrics(steady(f, 20, 0.1)) for f in (80, 81, 79)]
        self.assertEqual(stats.compare(base, tuned)[0], stats.IMPROVED)

    def test_clear_worse(self):
        """A big loss is WORSE."""
        # 80 fps versus 60 fps is clearly worse.
        base = [stats.run_metrics(steady(f, 20)) for f in (80, 81, 79)]
        tuned = [stats.run_metrics(steady(f, 20)) for f in (60, 61, 59)]
        self.assertEqual(stats.compare(base, tuned)[0], stats.WORSE)

    def test_noisy_small_difference_is_no_change(self):
        """A small shift inside the noise is NO CHANGE."""
        # Runs that differ a lot from each other (noisy) with a tiny shift must not be called an improvement.
        base = [stats.run_metrics(steady(f, 20)) for f in (50, 70, 60)]
        tuned = [stats.run_metrics(steady(f, 20)) for f in (52, 72, 62)]
        self.assertEqual(stats.compare(base, tuned)[0], stats.NO_CHANGE)

    def test_too_few_runs(self):
        """Fewer than 3 runs is NOT ENOUGH DATA, and the minimum cannot be lowered."""
        a = self.run_set(60, 2)
        v = stats.compare(a, a)
        self.assertEqual(v[0], stats.NOT_ENOUGH)
        self.assertEqual(stats.compare(a, a, min_runs=1)[0], stats.NOT_ENOUGH)   # minimum is 3, cannot be lowered

    def test_under_one_percent_ignored(self):
        """Changes under 1% are ignored."""
        # A 0.5% change is below the 1% minimum, so no change is claimed.
        base = [stats.run_metrics(steady(f, 20)) for f in (60.0, 60.0, 60.0)]
        tuned = [stats.run_metrics(steady(f, 20)) for f in (60.3, 60.3, 60.3)]
        self.assertEqual(stats.compare(base, tuned)[0], stats.NO_CHANGE)


class ReportTests(Tmp):
    """Tests for building and saving reports."""
    def make(self, prefix, fps_list, seconds=20):
        """Write fake log files with the given fps values and return their paths."""
        out = []
        for i, f in enumerate(fps_list):
            out.append(self.write("%s%d.csv" % (prefix, i), log_text(steady(f, seconds))))
        return out

    def test_report_improved_and_saved(self):
        """An improved report is built and saved in the data folder."""
        # Three baseline runs near 60 fps and three tuned runs near 90 fps.
        b = self.make("b", (60, 61, 59))
        t = self.make("t", (90, 91, 89))
        rep = report.build(b, t, warmup_s=2)
        self.assertEqual(rep["verdict"], "IMPROVED")
        j, txt = report.save(rep)
        self.assertTrue(j.startswith(os.environ["GAMETUNE_HOME"]) and os.path.isfile(txt))

    def test_two_runs_not_enough(self):
        """Two runs per side are not enough for a verdict."""
        b = self.make("b", (60, 61))
        t = self.make("t", (90, 91))
        self.assertEqual(report.build(b, t, warmup_s=2)["verdict"], "NOT ENOUGH DATA")

    def test_short_runs_excluded(self):
        """Runs that are too short are excluded with notes."""
        b = self.make("b", (60, 61, 59), seconds=4)
        t = self.make("t", (90, 91, 89), seconds=4)
        rep = report.build(b, t, warmup_s=2)
        self.assertEqual(rep["verdict"], "NOT ENOUGH DATA")
        self.assertTrue(rep["notes"])

    def test_hot_warning(self):
        """A hot GPU in the logs adds a warning."""
        b = [self.write("b%d.csv" % i, log_text(steady(60, 20), gpu_temp=88)) for i in range(3)]
        t = self.make("t", (60, 60, 60))
        self.assertTrue(report.build(b, t, warmup_s=2)["warnings"])


class ThermalTests(unittest.TestCase):
    """Tests for the thermal guard and the self-test safety paths."""
    def test_thresholds(self):
        """Thermal thresholds behave as documented."""
        # Short name for the function under test.
        E = thermal.evaluate
        self.assertEqual(E(84, 70, True)[0], thermal.OK)
        self.assertEqual(E(85, 70, True)[0], thermal.TOO_HOT_TO_START)
        self.assertEqual(E(85, 70, False)[0], thermal.OK)
        self.assertEqual(E(86, 70, False)[0], thermal.PAUSE)
        self.assertEqual(E(91, 70, False)[0], thermal.ABORT)
        self.assertEqual(E(60, 94, False)[0], thermal.PAUSE)
        self.assertEqual(E(60, 96, False)[0], thermal.ABORT)
        self.assertEqual(E(None, None, True)[0], thermal.UNKNOWN)
        self.assertEqual(E(70, None, True)[0], thermal.OK)

    def test_limits_only_stricter(self):
        """Limits can only be made stricter, never looser."""
        lim = thermal.Limits(gpu_start=99, gpu_pause=99, gpu_abort=99, cpu_abort=120)
        self.assertEqual((lim.gpu_start, lim.gpu_pause, lim.gpu_abort, lim.cpu_abort), (84, 86, 91, 96))
        self.assertEqual(thermal.evaluate(80, 50, True, thermal.Limits(gpu_start=70))[0], thermal.TOO_HOT_TO_START)

    def test_never_97(self):
        """A CPU at 97 C always aborts."""
        self.assertEqual(thermal.evaluate(50, 97, False)[0], thermal.ABORT)

    def test_selftest_refuses_hot(self):
        """The self-test refuses to start when the GPU is hot."""
        # Fake `which` says every tool exists; an 88 C GPU must stop the self-test before it starts.
        which = lambda n: "/bin/" + n
        r = selftest.run(which=which, sample=lambda: {"gpu_c": 88.0, "cpu_c": 50.0}, mangohud_path=None)
        self.assertEqual(r["status"], "REFUSED_HOT")

    def test_selftest_skips_when_missing(self):
        """The self-test skips (not fails) when tools are missing."""
        # With no tools installed the self-test is SKIPPED with a helpful message.
        r = selftest.run(which=lambda n: None, sample=lambda: {"gpu_c": 50.0, "cpu_c": 50.0})
        self.assertEqual(r["status"], "SKIPPED")
        text = " ".join(r["lines"])
        self.assertIn("SKIPPED, not failed", text)
        self.assertIn("sudo pacman -S --needed mangohud vkmark gamemode", text)
        self.assertIn("mangohud", r["missing"])

    def test_sensor_reading_fake_tree(self):
        """Sensor reading works on a fake hwmon tree."""
        with tempfile.TemporaryDirectory() as root:
            # Build a fake sysfs tree with a CPU sensor named coretemp.
            hw = os.path.join(root, "sys/class/hwmon/hwmon0")
            os.makedirs(hw)
            open(os.path.join(hw, "name"), "w").write("coretemp\n")
            open(os.path.join(hw, "temp1_input"), "w").write("71000\n")
            open(os.path.join(hw, "temp1_label"), "w").write("Package id 0\n")
            open(os.path.join(hw, "temp2_input"), "w").write("80000\n")
            self.assertEqual(thermal.read_cpu_temp(root), 71.0)
            self.assertIsNone(thermal.read_gpu_temp(root, runner=lambda a: None))


class HardwareTests(unittest.TestCase):
    """Tests for hardware detection on fake machines."""
    def build(self, root, hybrid=True):
        """Create the fake machine files (4 CPUs, an AMD GPU and an Intel GPU) under root."""
        def w(rel, text):
            p = os.path.join(root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").write(text)
        w("proc/cpuinfo", "".join("processor\t: %d\nmodel name\t: Fake CPU 9000\n\n" % i for i in range(4)))
        w("proc/meminfo", "MemTotal:       8388608 kB\n")
        for i in range(4):
            w("sys/devices/system/cpu/cpu%d/topology/core_id" % (i // 2), str(i // 2))
            w("sys/devices/system/cpu/cpu%d/topology/physical_package_id" % i, "0")
        if hybrid:
            w("sys/devices/cpu_core/cpus", "0-1")
            w("sys/devices/cpu_atom/cpus", "2-3")
        w("sys/class/drm/card0/device/vendor", "0x1002\n")
        w("sys/class/drm/card0/device/device", "0x73bf\n")
        w("sys/class/drm/card0/device/mem_info_vram_total", str(8 * 1024 ** 3))
        w("sys/class/drm/card1/device/vendor", "0x8086\n")
        w("sys/class/drm/card1/device/device", "0x46a6\n")

    def test_fake_tree(self):
        """A whole fake machine is scanned and checked field by field."""
        # Fake lspci output for the two GPUs.
        lspci = '00:02.0 "VGA compatible controller [0300]" "Intel Corporation [8086]" "Fake iGPU [46a6]" -r0c\n' \
                '03:00.0 "VGA compatible controller [0300]" "AMD [1002]" "Fake Radeon [73bf]" -r0c\n'
        with tempfile.TemporaryDirectory() as root:
            self.build(root)
            # Scan the fake machine with fake commands and a fake environment.
            info = hardware.scan(root=root, runner=lambda a: lspci if a[0] == "lspci" else None,
                                 environ={"XDG_SESSION_TYPE": "Wayland"}, which=lambda n: "/x/" + n if n == "gamescope" else None)
        self.assertEqual(info["cpu"]["model"], "Fake CPU 9000")
        self.assertEqual(info["cpu"]["logical_cpus"], 4)
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
        """A CPU without hybrid files is reported as non-hybrid."""
        with tempfile.TemporaryDirectory() as root:
            self.build(root, hybrid=False)
            self.assertFalse(hardware.detect_cpu(root)["hybrid"])

    def test_nvidia_smi_overrides(self):
        """nvidia-smi details override the sysfs values."""
        # Fake nvidia-smi output: name, memory in MiB, driver version.
        smi = "Fake RTX, 6144, 999.1\n"
        gpus = hardware.detect_gpus(root="/nonexistent", runner=lambda a: smi if a[0] == "nvidia-smi" else None)
        self.assertEqual((gpus[0]["vendor"], gpus[0]["vram_mib"], gpus[0]["kind"]), ("NVIDIA", 6144, "discrete"))

    def test_cpulist(self):
        """CPU list parsing and formatting work in both directions."""
        self.assertEqual(hardware.parse_cpulist("0-2,5"), [0, 1, 2, 5])
        self.assertEqual(hardware.format_cpulist([0, 1, 2, 5]), "0-2,5")
        self.assertEqual(hardware.parse_cpulist("junk"), [])

    def test_empty_root_does_not_crash(self):
        """Scanning an empty machine does not crash."""
        with tempfile.TemporaryDirectory() as root:
            info = hardware.scan(root=root, runner=lambda a: None, environ={}, which=lambda n: None)
            self.assertEqual(info["gpus"], [])
            self.assertEqual(info["session"]["type"], "unknown")


class ProfileTests(Tmp):
    """Tests for profiles, apply and revert, and the safety promises."""
    def tree(self):
        """Return {path: bytes} for every file under the temporary folder."""
        out = {}
        for d, _s, fs in os.walk(self.dir):
            for f in fs:
                p = os.path.join(d, f)
                out[p] = open(p, "rb").read()
        return out

    def test_dry_run_writes_nothing(self):
        """A dry run writes nothing."""
        before = self.tree()
        pl = profiles.plan("mygame", add=("fps-cap", {"fps": "60"}))
        self.assertTrue(pl["files"])
        self.assertEqual(self.tree(), before)
        self.assertFalse(os.path.exists(paths.home()))

    def test_cli_dry_run_and_yes(self):
        """The command line dry run, apply and revert work together."""
        from gametune import cli
        import io, contextlib
        # Capture what the command prints so the test can look at it.
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
        """Apply then revert restores the original bytes."""
        # A launch file the user already has; gametune must restore it byte for byte.
        launch = paths.sub("launch", "g2.launch.txt")
        os.makedirs(os.path.dirname(launch))
        with open(launch, "wb") as f:
            f.write(b"user's own text \xff\x00\n")
        before = self.tree()
        for name, params in (("fps-cap", {"fps": "75"}), ("render-scale", {"scale": "0.6", "output": "2560x1440"})):
            profiles.apply_plan("g2", profiles.plan("g2", add=(name, params)), "apply " + name)
        text = open(launch).read()
        self.assertIn("fps_limit=75", text)
        self.assertIn("gamescope -w 1536 -h 864 -W 2560 -H 1440 -F fsr", text)
        self.assertTrue(os.access(paths.sub("wrappers", "g2.sh"), os.X_OK))
        ok, msg = profiles.revert_game("g2")
        self.assertTrue(ok, msg)
        self.assertEqual(self.tree(), before)

    def test_revert_single_profile_rebuilds(self):
        """Reverting one profile rebuilds the launch line."""
        for name, params in (("fps-cap", {"fps": "75"}), ("shader-cache", {})):
            profiles.apply_plan("g3", profiles.plan("g3", add=(name, params), hw={"gpu_vendor": "NVIDIA"}), "a")
        ok, _ = profiles.revert_profile("g3", "fps-cap")
        self.assertTrue(ok)
        text = open(paths.sub("launch", "g3.launch.txt")).read()
        self.assertNotIn("fps_limit", text)
        self.assertIn("__GL_SHADER_DISK_CACHE_SKIP_CLEANUP=1", text)
        ok, _ = profiles.revert_profile("g3", "shader-cache")
        self.assertTrue(ok)
        self.assertEqual(self.tree(), {})

    def test_revert_refuses_if_user_edited(self):
        """Revert refuses when the user edited a gametune file."""
        profiles.apply_plan("g4", profiles.plan("g4", add=("fps-cap", {})), "a")
        with open(paths.sub("launch", "g4.launch.txt"), "a") as f:
            f.write("edited")
        ok, msg = profiles.revert_game("g4")
        self.assertFalse(ok)
        self.assertIn("--force", msg)
        self.assertTrue(profiles.revert_game("g4", force=True)[0])

    def test_unproven_hidden_and_guarded(self):
        """Unproven profiles are hidden and guarded."""
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
        """core-pinning uses the detected cores and refuses without them."""
        pl = profiles.plan("g6", add=("core-pinning", {}), hw={"performance_cpus": "0-3"})
        self.assertIn("taskset -c 0-3", list(pl["files"].values())[0][0].decode())
        with self.assertRaises(profiles.ProfileError):
            profiles.plan("g6", add=("core-pinning", {}), hw={})

    def test_bad_names_and_params(self):
        """Bad names and parameters raise ProfileError."""
        for bad in ("../x", "a/b", "", "-x", "a b"):
            with self.assertRaises(profiles.ProfileError):
                profiles.check_game(bad)
        with self.assertRaises(profiles.ProfileError):
            profiles.plan("g7", add=("fps-cap", {"fps": "abc"}))
        with self.assertRaises(profiles.ProfileError):
            profiles.plan("g7", add=("render-scale", {"scale": "0.1"}))

    def test_no_sudo_or_network_in_source(self):
        """The source contains no network, process-killing or privilege-raising words."""
        src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        blob = ""
        for f in os.listdir(os.path.join(src, "gametune")):
            if f.endswith(".py"):
                blob += open(os.path.join(src, "gametune", f)).read()
        # Words that would show network, process-killing or privilege-raising code.
        for word in ("import socket", "urllib", "http.client", "requests", "pkill", "killall", '"sudo"'):
            self.assertNotIn(word, blob, word)


# Run the tests when this file is started directly.
if __name__ == "__main__":
    unittest.main()
