# Program: gametune - hardware.py
# Programmer: Ethan-da-Tech-Wizard (written with Claude, an AI assistant)
# Last Modified: 2026-11-18
# Purpose: Detects this PC's CPU (including hybrid performance/efficiency cores), GPUs, RAM, session type and helper tools by reading /proc, /sys and a few read-only commands, and describes them in plain sentences.

"""Hardware detection. Reads /proc and /sys (under an optional fake root for tests); nothing is hard-coded."""
# os/re/shutil: file paths, pattern matching, and finding programs on PATH.
import os
import re
import shutil

# thermal provides run_text (safe command runner) and sample (temperature reading).
from . import thermal

# Helper programs gametune looks for.
HELPERS = ("mangohud", "gamescope", "gamemoderun", "vkmark", "vkcube")
# PCI vendor id (as read from sysfs) -> vendor name.
VENDORS = {"0x10de": "NVIDIA", "0x1002": "AMD", "0x8086": "Intel"}


def _read(path):
    """Return the stripped text of a file, or None if it cannot be read."""
    # Try to read a text file; any failure just gives None instead of crashing.
    try:
        with open(path, "r") as handle:
            # Strip the trailing newline and spaces.
            return handle.read().strip()
    except (OSError, UnicodeDecodeError):
        return None


def parse_cpulist(text):
    # Start with an empty answer; bad text will return it empty.
    """'0-11,14' -> [0..11, 14]. Bad text gives []."""
    result = []
    # Split on commas; `or ''` guards against None.
    for part in (text or "").strip().split(","):
        part = part.strip()
        if not part:
            # Skip empty pieces.
            continue
        try:
            # A piece with a dash is a range like 0-11; expand it into every number in it (hi+1 because range stops early).
            if "-" in part:
                lo, hi = part.split("-", 1)
                result.extend(range(int(lo), int(hi) + 1))
            else:
                # A single number like 14.
                result.append(int(part))
        except ValueError:
            # Non-numbers mean the text is bad, so give up with an empty list.
            return []
    # Remove duplicates and sort.
    return sorted(set(result))


def format_cpulist(cpus):
    """Turn a list of CPU numbers like [0,1,2,5] into the compact text '0-2,5'."""
    # Sort and de-duplicate first so neighbouring numbers are easy to spot.
    cpus = sorted(set(cpus))
    # parts will hold (first, last) pairs of consecutive runs.
    parts = []
    start = prev = None
    for cpu in cpus:
        # The first number starts a run.
        if start is None:
            start = prev = cpu
        # The next number continues the run when it is exactly one more.
        elif cpu == prev + 1:
            prev = cpu
        else:
            # Otherwise close the current run and start a new one.
            parts.append((start, prev))
            start = prev = cpu
    # Do not forget the last run.
    if start is not None:
        parts.append((start, prev))
    # Print a lone number as '5' and a run as '0-11', joined by commas.
    return ",".join(str(a) if a == b else "%d-%d" % (a, b) for a, b in parts)


def detect_cpu(root="/"):
    """Read CPU model, thread count, core count and any hybrid (performance/efficiency) split. `root` allows a fake file tree in tests."""
    # Result template: every field starts as unknown; detection fills them in.
    info = {"model": None, "logical_cpus": None, "physical_cores": None,
            "hybrid": False, "performance_cpus": None, "efficiency_cpus": None, "hybrid_source": None}
    # /proc/cpuinfo lists every logical CPU; under a fake root in tests.
    text = _read(os.path.join(root, "proc", "cpuinfo")) or ""
    logical = 0
    # Count 'processor' lines (= logical CPUs) and remember the first model name seen.
    for line in text.splitlines():
        key, _, value = line.partition(":")
        key = key.strip()
        if key == "processor":
            logical += 1
        elif key in ("model name", "Hardware", "Processor", "cpu model") and info["model"] is None and value.strip():
            info["model"] = value.strip()
    # Look at the sysfs CPU folder for cpu0, cpu1, ... directories.
    cpu_dir = os.path.join(root, "sys", "devices", "system", "cpu")
    cpu_ids = []
    try:
        # Pick folder names like cpu7 and keep the number; the regex ensures only digits follow 'cpu'.
        cpu_ids = sorted(int(n[3:]) for n in os.listdir(cpu_dir) if re.match(r"^cpu\d+$", n))
    except OSError:
        pass
    # Prefer the /proc count; fall back to the number of sysfs folders.
    info["logical_cpus"] = logical or (len(cpu_ids) or None)
    # Physical cores are the unique (package, core id) pairs; hyperthreads share a pair.
    cores = set()
    for cid in cpu_ids:
        core = _read(os.path.join(cpu_dir, "cpu%d" % cid, "topology", "core_id"))
        pkg = _read(os.path.join(cpu_dir, "cpu%d" % cid, "topology", "physical_package_id"))
        if core is not None:
            cores.add((pkg, core))
    # Only report a core count if we actually read some topology.
    if cores:
        info["physical_cores"] = len(cores)
    # Hybrid CPUs. Intel exposes cpu_core / cpu_atom; others expose differing cpu_capacity.
    p = parse_cpulist(_read(os.path.join(root, "sys", "devices", "cpu_core", "cpus")))
    e = parse_cpulist(_read(os.path.join(root, "sys", "devices", "cpu_atom", "cpus")))
    lp = parse_cpulist(_read(os.path.join(root, "sys", "devices", "cpu_lowpower", "cpus")))
    # Hybrid when we found performance cores AND some kind of efficiency/low-power cores.
    if p and (e or lp):
        info.update(hybrid=True, performance_cpus=format_cpulist(p),
                    efficiency_cpus=format_cpulist(e + lp), hybrid_source="sysfs cpu_core/cpu_atom")
    # Fallback: compare each CPU's cpu_capacity number.
    else:
        caps = {}
        for cid in cpu_ids:
            raw = _read(os.path.join(cpu_dir, "cpu%d" % cid, "cpu_capacity"))
            if raw is not None and raw.isdigit():
                caps[cid] = int(raw)
        # If the capacities differ, the highest ones are performance cores and the rest efficiency.
        if len(set(caps.values())) > 1:
            top = max(caps.values())
            info.update(hybrid=True,
                        performance_cpus=format_cpulist([c for c, v in caps.items() if v == top]),
                        efficiency_cpus=format_cpulist([c for c, v in caps.items() if v != top]),
                        hybrid_source="sysfs cpu_capacity")
    return info


def detect_ram(root="/"):
    """Read total memory (MiB) from /proc/meminfo; the value is None if it cannot be found."""
    # /proc/meminfo has a line 'MemTotal: <number> kB'.
    text = _read(os.path.join(root, "proc", "meminfo")) or ""
    # re.M lets ^ match at the start of any line.
    match = re.search(r"^MemTotal:\s+(\d+)\s*kB", text, re.M)
    total_kib = int(match.group(1)) if match else None
    # Convert kibibytes to mebibytes.
    return {"total_mib": total_kib // 1024 if total_kib else None}


def _parse_lspci(text):
    """Parse `lspci -mm -nn` lines for display controllers -> {'vendor:device': 'Vendor Device name'}."""
    # Maps 'vendor:device' ids to readable GPU names.
    names = {}
    for line in (text or "").splitlines():
        # Only display adapters matter (graphics cards).
        if not re.search(r"VGA compatible|3D controller|Display controller", line):
            continue
        # lspci -mm puts each field in double quotes; pull the quoted fields out.
        parts = re.findall(r'"([^"]*)"', line)
        if len(parts) < 3:
            continue
        # Hex ids look like [10de]; collect the vendor and device ids.
        ids = re.findall(r"\[([0-9a-f]{4})\]", parts[1] + " " + parts[2])
        if len(ids) < 2:
            continue
        # Strip the [xxxx] id tags from a name.
        clean = lambda s: re.sub(r"\s*\[[0-9a-f]{4}\]", "", s).strip()
        # Key is 'vendor:device' in lowercase, value is the cleaned vendor + device name.
        names[(ids[0] + ":" + ids[1]).lower()] = (clean(parts[1]) + " " + clean(parts[2])).strip()
    return names


def detect_gpus(root="/", runner=thermal.run_text):
    """List the GPUs (vendor, name, VRAM, driver, discrete/integrated guess). `runner` runs commands and can be faked in tests."""
    # runner is a parameter so tests can pass a fake instead of really running lspci.
    gpus = []
    names = _parse_lspci(runner(["lspci", "-mm", "-nn"]))
    # Linux lists graphics cards under /sys/class/drm as card0, card1, ...
    drm = os.path.join(root, "sys", "class", "drm")
    try:
        # Keep only plain cardN entries (not card0-HDMI-A-1) sorted by their number.
        cards = sorted((c for c in os.listdir(drm) if re.match(r"^card\d+$", c)), key=lambda c: int(c[4:]))
    except OSError:
        cards = []
    for card in cards:
        # Each card's details live in its 'device' subfolder.
        dev = os.path.join(drm, card, "device")
        vid = (_read(os.path.join(dev, "vendor")) or "").lower()
        did = (_read(os.path.join(dev, "device")) or "").lower()
        # No vendor id means this entry is not a real GPU, skip it.
        if not vid:
            continue
        # Total video memory in bytes (only AMD exposes this file).
        vram = _read(os.path.join(dev, "mem_info_vram_total"))
        # Lookup key into the lspci names.
        key = (vid[2:] + ":" + did[2:]).lower()
        # Build one record per GPU; VRAM is converted from bytes to MiB; driver is the name of the symlink target.
        gpus.append({"card": card, "vendor": VENDORS.get(vid, "Other (" + vid + ")"), "vendor_id": vid,
                     "name": names.get(key), "vram_mib": int(vram) // (1024 * 1024) if vram and vram.isdigit() else None,
                     "driver": os.path.basename(os.path.realpath(os.path.join(dev, "driver"))) if os.path.islink(os.path.join(dev, "driver")) else None,
                     "kind": None})
    # No drm cards found: fall back to whatever lspci reported.
    if not gpus:
        for key, name in names.items():
            vid = "0x" + key.split(":")[0]
            gpus.append({"card": None, "vendor": VENDORS.get(vid, "Other"), "vendor_id": vid, "name": name,
                         "vram_mib": None, "driver": None, "kind": None})
    # NVIDIA details from nvidia-smi (the driver's own report is more exact than sysfs).
    out = runner(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"])
    smi = []
    # Each output line is 'name, memory, driver version'.
    for line in (out or "").splitlines():
        cols = [c.strip() for c in line.split(",")]
        if len(cols) >= 3:
            try:
                smi.append((cols[0], int(float(cols[1])), cols[2]))
            except ValueError:
                pass
    # NVIDIA GPUs already found; match nvidia-smi rows to them in order.
    nv = [g for g in gpus if g["vendor"] == "NVIDIA"]
    for i, (name, vram, drv) in enumerate(smi):
        target = nv[i] if i < len(nv) else None
        # More smi rows than known cards: create a new record.
        if target is None:
            target = {"card": None, "vendor": "NVIDIA", "vendor_id": "0x10de", "name": None, "vram_mib": None,
                      "driver": None, "kind": None}
            gpus.append(target)
        # Overwrite with the more exact smi values.
        target.update(name=name, vram_mib=vram, driver="nvidia " + drv)
    # Finally guess whether each GPU is discrete or integrated.
    for g in gpus:
        if g["vendor"] == "NVIDIA":
            g["kind"] = "discrete"
        elif g["vendor"] == "AMD":
            # AMD: 2 GiB or more of VRAM suggests a discrete card; this is only a guess.
            g["kind"] = "discrete (guess)" if (g["vram_mib"] or 0) >= 2048 else "integrated (guess)"
        elif g["vendor"] == "Intel":
            g["kind"] = "integrated (guess)"
        else:
            g["kind"] = "unknown"
    return gpus


def detect_session(environ=None):
    """Report whether this is a Wayland or X11 session. `environ` can be a fake environment in tests."""
    # Prefer the standard XDG_SESSION_TYPE variable.
    env = os.environ if environ is None else environ
    stype = env.get("XDG_SESSION_TYPE") or None
    # Otherwise infer from which display variable exists (Wayland first, then X11).
    if not stype:
        stype = "wayland" if env.get("WAYLAND_DISPLAY") else ("x11" if env.get("DISPLAY") else "unknown")
    # Report the type plus whether each display variable is set.
    return {"type": stype.lower(), "wayland_display": bool(env.get("WAYLAND_DISPLAY")), "x_display": bool(env.get("DISPLAY"))}


def detect_helpers(which=shutil.which):
    """Report which helper programs (mangohud, gamescope, ...) are installed. `which` can be faked in tests."""
    # Map each helper name to its full path, or None if not installed.
    return {name: which(name) for name in HELPERS}


def scan(root="/", runner=thermal.run_text, environ=None, which=shutil.which):
    """Collect all hardware facts into one dictionary. The arguments let tests fake the machine."""
    # One temperature sample, used only to say whether the sensors can be read.
    sample = thermal.sample(root, runner)
    # Gather every detector into one dictionary.
    return {
        "cpu": detect_cpu(root),
        "gpus": detect_gpus(root, runner),
        "ram": detect_ram(root),
        "session": detect_session(environ),
        "helpers": detect_helpers(which),
        "thermal_sensors": {"gpu_readable": sample["gpu_c"] is not None, "cpu_readable": sample["cpu_c"] is not None},
    }


def primary_gpu_vendor(info):
    """Pick the GPU vendor that profiles should treat as the main one."""
    # Prefer the first discrete GPU's vendor.
    gpus = info.get("gpus") or []
    for g in gpus:
        if g.get("kind") == "discrete":
            return g["vendor"]
    # Otherwise the first GPU, or None when there are no GPUs.
    return gpus[0]["vendor"] if gpus else None


def describe(info):
    """Plain sentences, one fact per line."""
    # Build the output a sentence at a time.
    lines = []
    cpu = info["cpu"]
    # Show a clear fallback when the model is unknown.
    lines.append("CPU: %s." % (cpu["model"] or "model not found"))
    lines.append("CPU threads: %s. Physical cores: %s." % (cpu["logical_cpus"] if cpu["logical_cpus"] is not None else "unknown",
                                                          cpu["physical_cores"] if cpu["physical_cores"] is not None else "unknown"))
    # Say whether a performance/efficiency split was found, and how.
    if cpu["hybrid"]:
        lines.append("Hybrid CPU: yes. Performance threads: %s. Efficiency threads: %s. (found by %s)" % (
            cpu["performance_cpus"], cpu["efficiency_cpus"], cpu["hybrid_source"]))
    else:
        lines.append("Hybrid CPU: no (no performance and efficiency core split was found).")
    # One line per GPU, with unknown values spelled out.
    if not info["gpus"]:
        lines.append("GPU: none found.")
    for i, g in enumerate(info["gpus"], 1):
        # Show VRAM only if known.
        vram = "%d MiB video memory" % g["vram_mib"] if g["vram_mib"] else "video memory unknown"
        nm = g["name"] or "(model not found)"
        # Avoid repeating the vendor name when the model name already includes it.
        label = nm if g["vendor"].lower() in nm.lower() else "%s %s" % (g["vendor"], nm)
        lines.append("GPU %d: %s, %s, %s, driver %s." % (i, label, g["kind"], vram, g["driver"] or "unknown"))
    # RAM in MiB, or unknown.
    ram = info["ram"]["total_mib"]
    lines.append("Memory (RAM): %s." % ("%d MiB" % ram if ram else "unknown"))
    s = info["session"]
    lines.append("Session type: %s." % s["type"])
    # List each helper program and where it is installed.
    for name, path in info["helpers"].items():
        lines.append("Helper %s: %s." % (name, ("installed at " + path) if path else "NOT installed"))
    # Tell the user whether temperatures can be read.
    ts = info["thermal_sensors"]
    lines.append("GPU temperature sensor: %s." % ("readable" if ts["gpu_readable"] else "NOT readable"))
    lines.append("CPU temperature sensor: %s." % ("readable" if ts["cpu_readable"] else "NOT readable"))
    return lines
