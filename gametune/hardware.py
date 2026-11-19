# ==============================================================================
# Program: hardware.py
# Programmer: Ethan-da-Tech-Wizard
# Last Modified: November 18, 2026
# Purpose: Linux hardware inspection engine for CPU topologies, GPU cards, VRAM, and system RAM.
# ==============================================================================

"""Hardware detection. Reads /proc and /sys (under an optional fake root for tests); nothing is hard-coded."""

# File role: read-only discovery of CPU topology, GPUs, RAM, session type and helper programs. profiles.py consumes
# primary_gpu_vendor() and info["cpu"]["performance_cpus"]; cli.py prints describe(). Every reader takes a fake `root`
# (and injectable runner/environ/which) so tests/test_gametune.py can use fixture trees instead of real hardware.
# Design decisions: never guess silently; unknown values stay None and heuristics are labelled "(guess)". No command writes anything.
# Known limits: Linux only (/proc, /sys); VRAM size is read from sysfs for AMD only, other vendors rely on nvidia-smi.

import os
import re
import shutil

from . import thermal

# External programs gametune can use if present; scan() only reports whether each is on PATH.
# Only these five tools are ever looked for; gametune does not install anything, it just reports what is present.
HELPERS = ("mangohud", "gamescope", "gamemoderun", "vkmark", "vkcube")
# PCI vendor IDs as exposed by sysfs, mapped to display names.
VENDORS = {"0x10de": "NVIDIA", "0x1002": "AMD", "0x8086": "Intel"}


# Sysfs files vanish with hardware and kernel versions, so absence is treated as ordinary data rather than an error.
def _read(path):
    """Return stripped file text, or None when unreadable (missing sysfs nodes are normal)."""
    try:
        with open(path, "r") as handle:
            return handle.read().strip()
    except (OSError, UnicodeDecodeError):
        return None


def parse_cpulist(text):
    """'0-11,14' -> [0..11, 14]. Bad text gives []."""
    # Accumulate CPU numbers from each comma-separated piece ("0-3" or "7").
    # Collected as ints so the later sorted(set()) removes overlap between pieces such as "0-3,2".
    result = []
    for part in (text or "").strip().split(","):
        part = part.strip()
        if not part:
            continue
        try:
            # Ranges are inclusive on both ends, hence the +1 below.
            if "-" in part:
                lo, hi = part.split("-", 1)
                result.extend(range(int(lo), int(hi) + 1))
            else:
                result.append(int(part))
        except ValueError:
            # Any malformed piece invalidates the whole list; a partial list could pin games to the wrong cores.
            return []
    # De-duplicate and sort so equal lists compare equal.
    return sorted(set(result))


def format_cpulist(cpus):
    """Inverse of parse_cpulist: [0,1,2,5] -> '0-2,5' (the taskset/cpuset syntax)."""

    # Sorting first is what lets the loop below detect consecutive runs.
    cpus = sorted(set(cpus))
    parts = []
    # Track the current run of consecutive CPU numbers.
    start = prev = None
    for cpu in cpus:
        if start is None:
            start = prev = cpu
        # Still consecutive: extend the run.
        elif cpu == prev + 1:
            prev = cpu
        else:
            # A gap ends the run; begin a new one.
            parts.append((start, prev))
            start = prev = cpu
    if start is not None:
        parts.append((start, prev))
    return ",".join(str(a) if a == b else "%d-%d" % (a, b) for a, b in parts)


def detect_cpu(root="/"):
    """Return CPU model, thread/core counts and hybrid (P/E core) layout.

    `root` is a fake filesystem root so tests can feed fixture trees.
    """
    # Every field starts as unknown (None) so callers can tell "not found" from zero.
    # Whole result shape is fixed up front, so describe() and profiles.py can index keys without checking they exist.
    info = {"model": None, "logical_cpus": None, "physical_cores": None,
            "hybrid": False, "performance_cpus": None, "efficiency_cpus": None, "hybrid_source": None}
    # Count "processor" lines for threads; the model-name key differs between x86 ("model name") and ARM ("Hardware"/"Processor").
    text = _read(os.path.join(root, "proc", "cpuinfo")) or ""
    # Number of "processor" entries in /proc/cpuinfo = logical CPU threads.
    logical = 0

    # Only the first non-empty model string is kept: every logical CPU repeats the same line in /proc/cpuinfo.
    for line in text.splitlines():
        key, _, value = line.partition(":")
        key = key.strip()
        if key == "processor":
            logical += 1
        elif key in ("model name", "Hardware", "Processor", "cpu model") and info["model"] is None and value.strip():
            info["model"] = value.strip()
    cpu_dir = os.path.join(root, "sys", "devices", "system", "cpu")
    cpu_ids = []
    try:
        cpu_ids = sorted(int(n[3:]) for n in os.listdir(cpu_dir) if re.match(r"^cpu\d+$", n))
    except OSError:
        pass
    # Prefer the cpuinfo count; fall back to counting cpuN directories in sysfs.
    info["logical_cpus"] = logical or (len(cpu_ids) or None)
    # Physical cores = distinct (package, core_id) pairs, since SMT siblings share a core_id.
    # A set of (package, core_id) pairs de-duplicates SMT siblings, which share one core_id but are separate threads.
    cores = set()
    for cid in cpu_ids:
        core = _read(os.path.join(cpu_dir, "cpu%d" % cid, "topology", "core_id"))
        # Include the package id so core 0 of socket 0 and core 0 of socket 1 count separately.
        pkg = _read(os.path.join(cpu_dir, "cpu%d" % cid, "topology", "physical_package_id"))
        if core is not None:
            cores.add((pkg, core))
    if cores:
        info["physical_cores"] = len(cores)
    # Hybrid CPUs. Intel exposes cpu_core / cpu_atom; others expose differing cpu_capacity.
    # Intel hybrid CPUs (12th gen and later) expose the core types as separate PMU devices; that is the most reliable source, so it is tried first.
    p = parse_cpulist(_read(os.path.join(root, "sys", "devices", "cpu_core", "cpus")))
    e = parse_cpulist(_read(os.path.join(root, "sys", "devices", "cpu_atom", "cpus")))
    lp = parse_cpulist(_read(os.path.join(root, "sys", "devices", "cpu_lowpower", "cpus")))
    # Hybrid only if there is a performance set AND some efficiency/low-power set; a lone P-core list is a normal CPU.
    if p and (e or lp):
        info.update(hybrid=True, performance_cpus=format_cpulist(p),
                    efficiency_cpus=format_cpulist(e + lp), hybrid_source="sysfs cpu_core/cpu_atom")
    else:
        # Fallback for non-Intel hybrids (e.g. ARM big.LITTLE): cores with a lower cpu_capacity are the efficiency cores.
        # cpu_capacity is a kernel scheduler value (1024 = biggest core); used where the Intel nodes do not exist.
        caps = {}
        for cid in cpu_ids:
            raw = _read(os.path.join(cpu_dir, "cpu%d" % cid, "cpu_capacity"))
            if raw is not None and raw.isdigit():
                caps[cid] = int(raw)
        # Differing capacities mean an asymmetric CPU; the largest value marks the performance cores.
        if len(set(caps.values())) > 1:
            top = max(caps.values())
            info.update(hybrid=True,
                        performance_cpus=format_cpulist([c for c, v in caps.items() if v == top]),
                        efficiency_cpus=format_cpulist([c for c, v in caps.items() if v != top]),
                        hybrid_source="sysfs cpu_capacity")
    return info


def detect_ram(root="/"):
    """Total RAM in MiB from /proc/meminfo (MemTotal is reported in kB), or None."""
    text = _read(os.path.join(root, "proc", "meminfo")) or ""

    # re.M so ^ matches at line start. /proc/meminfo always reports kB, which are really KiB; dividing by 1024 gives MiB.
    match = re.search(r"^MemTotal:\s+(\d+)\s*kB", text, re.M)
    total_kib = int(match.group(1)) if match else None
    return {"total_mib": total_kib // 1024 if total_kib else None}


def _parse_lspci(text):
    """Parse `lspci -mm -nn` lines for display controllers -> {'vendor:device': 'Vendor Device name'}."""
    # Maps "vendorid:deviceid" -> readable name, so sysfs IDs can be joined to lspci names.
    # Needed because sysfs gives only numeric PCI ids, while lspci has the readable model names.
    names = {}
    for line in (text or "").splitlines():
        # Only display adapters; skip every other PCI device.
        if not re.search(r"VGA compatible|3D controller|Display controller", line):
            continue
        # -mm output is a list of quoted fields: class, vendor, device, ...
        parts = re.findall(r'"([^"]*)"', line)
        if len(parts) < 3:
            continue
        # In `lspci -mm -nn` the vendor and device names each end with a bracketed 4-hex-digit ID.
        ids = re.findall(r"\[([0-9a-f]{4})\]", parts[1] + " " + parts[2])
        if len(ids) < 2:
            continue
        # Remove the trailing "[xxxx]" ID so only the human name is kept.
        clean = lambda s: re.sub(r"\s*\[[0-9a-f]{4}\]", "", s).strip()
        names[(ids[0] + ":" + ids[1]).lower()] = (clean(parts[1]) + " " + clean(parts[2])).strip()
    return names


def detect_gpus(root="/", runner=thermal.run_text):
    """List GPUs using sysfs (vendor, VRAM, driver), lspci (names) and nvidia-smi.

    Each entry is a dict; `kind` is "discrete"/"integrated" and marked "(guess)"
    when inferred rather than reported by the driver.
    """
    gpus = []
    names = _parse_lspci(runner(["lspci", "-mm", "-nn"]))

    # /sys/class/drm lists "card0", "card0-HDMI-A-1" and so on; the regex below keeps only the card nodes themselves, not the connectors.
    drm = os.path.join(root, "sys", "class", "drm")
    try:
        cards = sorted((c for c in os.listdir(drm) if re.match(r"^card\d+$", c)), key=lambda c: int(c[4:]))
    except OSError:
        cards = []
    # Iterate card0, card1, ... in numeric order (so card10 sorts after card2).
    for card in cards:
        dev = os.path.join(drm, card, "device")
        vid = (_read(os.path.join(dev, "vendor")) or "").lower()
        did = (_read(os.path.join(dev, "device")) or "").lower()
        # No vendor file: not a real PCI GPU node (for example a virtual device).
        if not vid:
            continue
        # mem_info_vram_total (bytes) exists for amdgpu only; other vendors leave VRAM unknown here.
        vram = _read(os.path.join(dev, "mem_info_vram_total"))
        key = (vid[2:] + ":" + did[2:]).lower()

        # Every value defaults to None rather than 0 so describe() prints "unknown" instead of a made-up number.
        gpus.append({"card": card, "vendor": VENDORS.get(vid, "Other (" + vid + ")"), "vendor_id": vid,
                     "name": names.get(key), "vram_mib": int(vram) // (1024 * 1024) if vram and vram.isdigit() else None,
                     "driver": os.path.basename(os.path.realpath(os.path.join(dev, "driver"))) if os.path.islink(os.path.join(dev, "driver")) else None,
                     "kind": None})
    # No DRM cards visible (e.g. container): fall back to whatever lspci listed.
    if not gpus:
        for key, name in names.items():
            vid = "0x" + key.split(":")[0]
            gpus.append({"card": None, "vendor": VENDORS.get(vid, "Other"), "vendor_id": vid, "name": name,
                         "vram_mib": None, "driver": None, "kind": None})
    # NVIDIA details from nvidia-smi (the driver's own report is more exact than sysfs).
    # nvidia-smi is the only source of VRAM for NVIDIA cards (sysfs does not expose it); nounits makes the number plain MiB.
    out = runner(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"])
    smi = []
    for line in (out or "").splitlines():
        cols = [c.strip() for c in line.split(",")]
        if len(cols) >= 3:
            try:
                smi.append((cols[0], int(float(cols[1])), cols[2]))
            except ValueError:
                pass
    # nvidia-smi rows are matched to sysfs NVIDIA cards by position.
    nv = [g for g in gpus if g["vendor"] == "NVIDIA"]
    for i, (name, vram, drv) in enumerate(smi):
        target = nv[i] if i < len(nv) else None
        if target is None:
            target = {"card": None, "vendor": "NVIDIA", "vendor_id": "0x10de", "name": None, "vram_mib": None,
                      "driver": None, "kind": None}
            gpus.append(target)
        # Overwrite the sysfs guesses with the driver's own numbers.
        target.update(name=name, vram_mib=vram, driver="nvidia " + drv)
    # Classify. NVIDIA is treated as discrete; AMD with >= 2 GiB VRAM is guessed discrete; Intel is integrated. These are heuristics, hence "(guess)".
    # Discrete GPUs come first in the order they were found, so a laptop reports its dGPU rather than its integrated GPU for profile choices.
    for g in gpus:
        if g["vendor"] == "NVIDIA":
            g["kind"] = "discrete"
        elif g["vendor"] == "AMD":
            g["kind"] = "discrete (guess)" if (g["vram_mib"] or 0) >= 2048 else "integrated (guess)"
        elif g["vendor"] == "Intel":
            g["kind"] = "integrated (guess)"
        else:
            g["kind"] = "unknown"
    return gpus


def detect_session(environ=None):
    """Detect X11 vs Wayland from XDG_SESSION_TYPE, falling back to display variables."""
    env = os.environ if environ is None else environ

    # The session type matters because overlays and gamescope behave differently on Wayland and X11.
    stype = env.get("XDG_SESSION_TYPE") or None
    if not stype:
        # Heuristic fallback when the session type variable is not set.
        stype = "wayland" if env.get("WAYLAND_DISPLAY") else ("x11" if env.get("DISPLAY") else "unknown")
    return {"type": stype.lower(), "wayland_display": bool(env.get("WAYLAND_DISPLAY")), "x_display": bool(env.get("DISPLAY"))}


def detect_helpers(which=shutil.which):
    """Map each helper name to its path on PATH, or None if missing."""

    # `which` is injectable so tests can pretend tools are installed or missing without touching PATH.
    return {name: which(name) for name in HELPERS}


def scan(root="/", runner=thermal.run_text, environ=None, which=shutil.which):
    """Collect everything gametune knows about this PC into one dict.

    All inputs (fake root, command runner, environment, `which`) are injectable
    so the whole scan can be tested without real hardware.
    """
    # Only used to learn whether sensors are readable; the temperatures themselves are not stored.
    # Passing root and runner through keeps the whole scan fake-able in one call.
    sample = thermal.sample(root, runner)
    return {
        "cpu": detect_cpu(root),
        "gpus": detect_gpus(root, runner),
        "ram": detect_ram(root),
        "session": detect_session(environ),
        "helpers": detect_helpers(which),
        "thermal_sensors": {"gpu_readable": sample["gpu_c"] is not None, "cpu_readable": sample["cpu_c"] is not None},
    }


def primary_gpu_vendor(info):
    """Vendor of the first discrete GPU, else of the first GPU, else None."""
    gpus = info.get("gpus") or []
    for g in gpus:
        if g.get("kind") == "discrete":
            return g["vendor"]
    return gpus[0]["vendor"] if gpus else None


def describe(info):
    """Plain sentences, one fact per line."""
    lines = []
    cpu = info["cpu"]
    lines.append("CPU: %s." % (cpu["model"] or "model not found"))
    lines.append("CPU threads: %s. Physical cores: %s." % (cpu["logical_cpus"] if cpu["logical_cpus"] is not None else "unknown",
                                                          cpu["physical_cores"] if cpu["physical_cores"] is not None else "unknown"))
    if cpu["hybrid"]:
        lines.append("Hybrid CPU: yes. Performance threads: %s. Efficiency threads: %s. (found by %s)" % (
            cpu["performance_cpus"], cpu["efficiency_cpus"], cpu["hybrid_source"]))
    else:
        lines.append("Hybrid CPU: no (no performance and efficiency core split was found).")
    if not info["gpus"]:
        lines.append("GPU: none found.")
    for i, g in enumerate(info["gpus"], 1):
        vram = "%d MiB video memory" % g["vram_mib"] if g["vram_mib"] else "video memory unknown"
        nm = g["name"] or "(model not found)"
        # Avoid printing the vendor twice ("AMD AMD Radeon ...").
        # Names from lspci often already start with the vendor, so prefixing it again would print "AMD AMD".
        label = nm if g["vendor"].lower() in nm.lower() else "%s %s" % (g["vendor"], nm)
        lines.append("GPU %d: %s, %s, %s, driver %s." % (i, label, g["kind"], vram, g["driver"] or "unknown"))
    ram = info["ram"]["total_mib"]
    lines.append("Memory (RAM): %s." % ("%d MiB" % ram if ram else "unknown"))
    s = info["session"]
    lines.append("Session type: %s." % s["type"])
    for name, path in info["helpers"].items():
        lines.append("Helper %s: %s." % (name, ("installed at " + path) if path else "NOT installed"))
    ts = info["thermal_sensors"]
    lines.append("GPU temperature sensor: %s." % ("readable" if ts["gpu_readable"] else "NOT readable"))
    lines.append("CPU temperature sensor: %s." % ("readable" if ts["cpu_readable"] else "NOT readable"))
    return lines
