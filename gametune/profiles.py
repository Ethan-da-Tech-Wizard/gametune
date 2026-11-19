# ==============================================================================
# Program: profiles.py
# Programmer: Ethan-da-Tech-Wizard
# Last Modified: November 18, 2026
# Purpose: Reversible PC optimization profiles (process priority, memory compaction, shader cache).
# ==============================================================================

"""Profiles: small, named, undoable launch-option recipes.

gametune only ever writes text files under its own data folder. It never edits game files or system files,
never uses sudo, never kills programs. You paste the launch string into Steam yourself (or run the wrapper).

Evidence labels are honest:
  "established"  The mechanism is basic (fewer pixels or fewer frames means less work). Still, nothing is
                 proven on YOUR PC until `gametune report` says IMPROVED for your game.
  "unproven"     Folklore or tiny effect. OFF unless you pass --experimental. Test it with `gametune report`.
"""
import base64
import hashlib
import json
import os
import re
import shlex
import time

from . import paths, thermal

# Game names become file names under gametune's data folder, so the pattern
# forbids slashes, leading dots and anything else that could escape that
# folder (path traversal) or confuse a shell.
GAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ProfileError(Exception):
    """User-facing error: the message is a plain sentence printed by the CLI."""
    pass


class Piece(object):
    """What one profile adds to the launch line."""

    def __init__(self):
        self.env = {}
        self.prefix = []        # commands before gamescope (gamemoderun, taskset ...)
        self.gamescope = []     # gamescope arguments
        self.mangohud = []      # MangoHud config options
        self.notes = []


def _even(x):
    """Round to the nearest integer, then down to an even number.

    Scaled resolutions are kept even because some video paths reject odd sizes.
    """
    x = int(round(x))
    return x - (x % 2)


def _render_scale(params, hw):
    """Builder for "render-scale": gamescope draws small and upscales with FSR 1.

    Validates --output (WxH) and --scale (0.3 to 1.0) before building anything.
    """
    out = params.get("output", "1920x1080")
    m = re.match(r"^(\d{3,5})x(\d{3,5})$", out)
    if not m:
        raise ProfileError("--output must look like 1920x1080.")
    W, H = int(m.group(1)), int(m.group(2))
    try:
        s = float(params.get("scale", "0.75"))
    except ValueError:
        raise ProfileError("--scale must be a number such as 0.75.")
    if not 0.3 <= s <= 1.0:
        raise ProfileError("--scale must be between 0.3 and 1.0.")
    p = Piece()
    # gamescope flags: -w/-h = game (internal) size, -W/-H = output size, -F fsr = FSR 1 upscaler.
    p.gamescope = ["-w", str(_even(W * s)), "-h", str(_even(H * s)), "-W", str(W), "-H", str(H), "-F", "fsr"]
    p.notes.append("The game draws at %dx%d and gamescope scales it up to %dx%d with FSR 1. Sharpness drops a little." % (_even(W * s), _even(H * s), W, H))
    return p


def _fps_cap(params, hw):
    """Builder for "fps-cap": a MangoHud fps_limit between 20 and 1000."""
    try:
        n = int(params.get("fps", "60"))
    except ValueError:
        raise ProfileError("--fps must be a whole number such as 60.")
    if not 20 <= n <= 1000:
        raise ProfileError("--fps must be between 20 and 1000.")
    p = Piece()
    p.mangohud = ["fps_limit=%d" % n]
    p.notes.append("Caps the game at %d FPS using MangoHud. A cap mostly makes frame times steadier and the PC cooler; it does not raise FPS." % n)
    return p


def _measure(params, hw):
    """Builder for "measure": configure MangoHud to auto-log frame times.

    autostart_log is the delay in seconds before logging begins, log_duration
    the length, and log_interval=0 means one row per frame (not sampled).
    """
    p = Piece()
    folder = params.get("log_folder") or paths.sub("logs")
    p.mangohud = ["output_folder=" + folder, "autostart_log=%s" % params.get("delay", "5"),
                  "log_duration=%s" % params.get("seconds", "90"), "log_interval=0"]
    p.notes.append("MangoHud writes a frame-time log to %s. Use at least 3 runs per side with `gametune report`." % folder)
    return p


def _shader_cache(params, hw):
    """Builder for "shader-cache": stop the driver trimming its shader cache.

    Chooses the NVIDIA or Mesa environment variable from the detected GPU vendor.
    """
    p = Piece()
    vendor = (hw or {}).get("gpu_vendor")
    if vendor == "NVIDIA":
        p.env["__GL_SHADER_DISK_CACHE_SKIP_CLEANUP"] = "1"
        p.notes.append("NVIDIA: stops the driver from trimming its shader cache, which can reduce repeated stutter. It does not raise average FPS.")
    else:
        p.env["MESA_SHADER_CACHE_MAX_SIZE"] = "10G"
        p.notes.append("Mesa (AMD/Intel): lets the shader cache grow to 10 GB so it is not trimmed. It does not raise average FPS.")
    return p


def _dxvk_async(params, hw):
    """Builder for the unproven "dxvk-async" profile (DXVK_ASYNC=1)."""
    p = Piece()
    p.env["DXVK_ASYNC"] = "1"
    p.notes.append("Unproven. It belongs to a patched DXVK and may do nothing on current Proton. Measure before keeping it.")
    return p


def _gamemode(params, hw):
    """Builder for the unproven "gamemode" profile: prefixes the launch with gamemoderun."""
    p = Piece()
    p.prefix = ["gamemoderun"]
    p.notes.append("Unproven. Published gains are small and often inside normal noise. Needs the gamemode package.")
    return p


def _core_pinning(params, hw):
    """Builder for the unproven "core-pinning" profile: taskset onto performance cores.

    Refuses (ProfileError) when no hybrid CPU was detected, so it never writes a meaningless taskset line.
    """
    p = Piece()
    perf = (hw or {}).get("performance_cpus")
    if not perf:
        raise ProfileError("core-pinning needs a hybrid CPU that `gametune scan` can see (performance and efficiency cores). "
                           "None was found on this PC, so nothing is written.")
    p.prefix = ["taskset", "-c", perf]
    p.notes.append("Unproven. Pins the game to the performance threads (%s). Modern kernels usually schedule hybrid CPUs well; this can even hurt." % perf)
    return p


class Profile(object):
    """Registry entry describing one profile.

    name/summary are shown to the user; evidence is "established" or "unproven";
    builder returns a Piece (None for advice-only); kind is "launch", "tool" or
    "advice"; default_on False hides it unless --experimental; params is help text.
    """
    def __init__(self, name, summary, evidence, builder, kind="launch", default_on=True, params=""):
        self.name, self.summary, self.evidence, self.builder = name, summary, evidence, builder
        self.kind, self.default_on, self.params = kind, default_on, params


# Registry of every profile, keyed by name. The list order is not significant.
PROFILES = {p.name: p for p in [
    Profile("render-scale", "Draw the game at a lower resolution and scale it up (gamescope + FSR 1). Fewer pixels means less GPU work.",
            "established", _render_scale, params="--scale 0.75 --output 1920x1080"),
    Profile("fps-cap", "Cap the frame rate (MangoHud). Steadier frame times and a cooler PC. Does not raise FPS.",
            "established", _fps_cap, params="--fps 60"),
    Profile("measure", "Record frame times with MangoHud so `gametune report` has real data.",
            "established", _measure, kind="tool", params="(none)"),
    Profile("shader-cache", "Keep the driver's shader cache from being trimmed. Helps repeat stutter only.",
            "established", _shader_cache, params="(none)"),
    Profile("close-apps", "ADVICE ONLY: lists heavy programs that are running so YOU can close them. Nothing is closed for you.",
            "established", None, kind="advice", params="(none)"),
    Profile("dxvk-async", "Set DXVK_ASYNC=1.", "unproven", _dxvk_async, default_on=False),
    Profile("gamemode", "Run the game through GameMode.", "unproven", _gamemode, default_on=False),
    Profile("core-pinning", "Pin the game to the performance cores of a hybrid CPU.", "unproven", _core_pinning, default_on=False),
]}

# Ideas deliberately left out, with the reason; shown by `gametune profiles`.
NOT_OFFERED = [("nice / process priority", "Raising priority needs administrator rights. gametune never uses sudo, so it is not offered.")]

# Process-name prefixes of common memory-hungry programs for the advice-only close-apps profile.
HEAVY_APPS = ("firefox", "chrome", "chromium", "brave", "vivaldi", "discord", "slack", "teams", "spotify", "obs", "thunderbird", "zoom", "code")


def list_profiles(experimental=False):
    """Profiles to show: default-on ones, plus unproven ones if `experimental`."""
    return [p for p in PROFILES.values() if p.default_on or experimental]


def running_heavy_apps(root="/"):
    """Read-only look at /proc for well-known memory-hungry programs. Returns [(name, rss_mib)]."""
    found = {}
    proc = os.path.join(root, "proc")
    try:
        entries = [e for e in os.listdir(proc) if e.isdigit()]
    except OSError:
        return []
    # /proc/PID/statm counts pages; assumes the usual 4 KiB page size.
    page_mib = 4096.0 / (1024 * 1024)
    for pid in entries:
        try:
            with open(os.path.join(proc, pid, "comm")) as f:
                comm = f.read().strip().lower()
            if not any(h == comm or comm.startswith(h) for h in HEAVY_APPS):
                continue
            # Second statm field is resident set size in pages.
            with open(os.path.join(proc, pid, "statm")) as f:
                rss = int(f.read().split()[1]) * page_mib
        # Processes can exit mid-scan; just skip any that vanish or have odd contents.
        except (OSError, ValueError, IndexError):
            continue
        found[comm] = found.get(comm, 0.0) + rss
    return sorted(((k, round(v)) for k, v in found.items()), key=lambda kv: -kv[1])


def check_game(name):
    """Validate a game name against GAME_RE and return it, or raise ProfileError."""
    if not GAME_RE.match(name or ""):
        raise ProfileError("Game name may only use letters, digits, dot, dash and underscore (max 64), and must start with a letter or digit.")
    return name


def compose(selected, hw):
    """selected: {profile name: params}. Returns (launch_options string, wrapper script text, notes)."""
    # Accumulators: environment variables, command prefixes, gamescope args, MangoHud options, user notes.
    env, prefix, gs, mh, notes = {}, [], [], [], []
    # Sorted so the generated line is deterministic regardless of dict order.
    for name in sorted(selected):
        prof = PROFILES[name]
        if prof.builder is None:
            continue
        piece = prof.builder(selected[name], hw)
        env.update(piece.env)
        prefix += piece.prefix
        gs += piece.gamescope
        mh += piece.mangohud
        notes += piece.notes
    # MangoHud takes all options through one MANGOHUD_CONFIG variable.
    if mh:
        env["MANGOHUD_CONFIG"] = ",".join(mh)
    parts = []
    for k in sorted(env):
        parts.append("%s=%s" % (k, shlex.quote(env[k])))
    parts += [shlex.quote(x) for x in prefix]
    if gs:
        parts += ["gamescope"] + [shlex.quote(x) for x in gs] + ["--"]
    if mh:
        parts.append("mangohud")
    # %command% is Steam's placeholder for the game's own launch command.
    steam = " ".join(parts + ["%command%"])
    # Same recipe again as a standalone shell wrapper (for non-Steam use); every value is shlex-quoted.
    wrap_env = ["%s=%s" % (k, shlex.quote(env[k])) for k in sorted(env)]
    wparts = (["env"] + wrap_env if wrap_env else []) + [shlex.quote(x) for x in prefix]
    if gs:
        wparts += ["gamescope"] + [shlex.quote(x) for x in gs] + ["--"]
    if mh:
        wparts.append("mangohud")
    wrapper = "#!/bin/sh\n# Written by gametune. Undo with: gametune revert --all\nexec %s\n" % " ".join(wparts + ['"$@"'])
    return steam + "\n", wrapper, notes


def _sha(data):
    """Hex SHA-256 of bytes; used to detect files edited after gametune wrote them."""
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path):
    """Read a file as bytes, or None if it does not exist or is unreadable."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _targets(game):
    """The four files gametune manages for a game (launch line, wrapper, state, advice)."""
    return {
        "launch": paths.sub("launch", game + ".launch.txt"),
        "wrapper": paths.sub("wrappers", game + ".sh"),
        "state": paths.sub("state", game + ".json"),
        "notes": paths.sub("advice", game + ".txt"),
    }


def _undo_path(game):
    """Location of the per-game undo journal."""
    return paths.sub("undo", game + ".json")


def _write_atomic(path, data, mode=None):
    """Write via temp file + os.replace so a crash never leaves a half-written file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def load_state(game):
    """Return the saved set of applied profiles for a game (empty if none)."""
    raw = _read_bytes(_targets(game)["state"])
    if raw is None:
        return {"game": game, "profiles": {}}
    return json.loads(raw.decode("utf-8"))


def plan(game, add=None, remove=None, hw=None, root="/"):
    """Work out the files that would be written. Pure; writes nothing. Returns dict path -> (bytes, mode) and summary."""
    check_game(game)
    state = load_state(game)
    # Copy the saved profile set, then apply the requested add/remove to the copy.
    profiles = dict(state.get("profiles", {}))
    if add:
        profiles[add[0]] = add[1]
    if remove:
        profiles.pop(remove, None)
    t = _targets(game)
    files = {}
    notes = []
    # With no profiles left, no files are planned; apply_plan then deletes the old ones.
    if profiles:
        steam, wrapper, notes = compose(profiles, hw)
        files[t["launch"]] = (steam.encode("utf-8"), 0o644)
        files[t["wrapper"]] = (wrapper.encode("utf-8"), 0o755)
        if "close-apps" in profiles:
            apps = running_heavy_apps(root)
            body = "Close these before playing (gametune never closes anything for you):\n" + (
                "".join("  %s  about %d MiB\n" % a for a in apps) if apps else "  Nothing well-known was running.\n")
            files[t["notes"]] = (body.encode("utf-8"), 0o644)
        state_out = {"game": game, "profiles": profiles, "hardware_used": {k: v for k, v in (hw or {}).items() if v}}
        files[t["state"]] = ((json.dumps(state_out, indent=2, sort_keys=True) + "\n").encode("utf-8"), 0o644)
    return {"files": files, "profiles": profiles, "notes": notes, "targets": t}


def apply_plan(game, pl, label):
    """Snapshot every file we are about to touch, then write. Records an undo entry first."""
    t = pl["targets"]
    all_paths = list(t.values())
    # Load the existing undo journal, or start a new one.
    undo = json.loads(_read_bytes(_undo_path(game)).decode("utf-8")) if _read_bytes(_undo_path(game)) else {"game": game, "records": []}
    # Snapshot (base64 of the exact bytes, or None for "did not exist") of every managed file before writing.
    snap = {}
    for p in all_paths:
        data = _read_bytes(p)
        snap[p] = None if data is None else base64.b64encode(data).decode("ascii")
    after = {p: _sha(d) for p, (d, _m) in pl["files"].items()}
    undo["records"].append({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "label": label, "before": snap, "after_sha256": after})
    _write_atomic(_undo_path(game), (json.dumps(undo, indent=2) + "\n").encode("utf-8"))   # undo record first
    for p in all_paths:
        if p in pl["files"]:
            data, mode = pl["files"][p]
            _write_atomic(p, data, mode)
        elif os.path.exists(p):
            os.remove(p)       # a file gametune made earlier that this profile set no longer needs


def revert_game(game, force=False):
    """Restore the exact bytes (or absence) of every file as they were before the first apply."""
    check_game(game)
    up = _undo_path(game)
    raw = _read_bytes(up)
    if raw is None:
        return False, "Nothing to undo for %s." % game
    undo = json.loads(raw.decode("utf-8"))
    # Safety check: if the user edited a file after our last write, refuse unless --force, so we never destroy their changes.
    last = undo["records"][-1] if undo["records"] else None
    if last and not force:
        for p, digest in last["after_sha256"].items():
            cur = _read_bytes(p)
            if cur is None or _sha(cur) != digest:
                return False, "%s changed after gametune wrote it. Nothing was restored. Use --force to restore anyway." % p
    # Restore the state from before the FIRST apply, which undoes every later apply at once.
    first = undo["records"][0]["before"] if undo["records"] else {}
    for p, b64 in first.items():
        if b64 is None:
            if os.path.exists(p):
                os.remove(p)
        else:
            _write_atomic(p, base64.b64decode(b64))
    os.remove(up)
    return True, "Restored %s to exactly how it was before gametune touched it." % game


def revert_profile(game, name, hw=None, force=False):
    """Remove one profile; if it was the last, restore everything via revert_game."""
    state = load_state(game)
    if name not in state.get("profiles", {}):
        return False, "Profile %s is not applied to %s." % (name, game)
    if len(state["profiles"]) == 1:
        return revert_game(game, force)
    pl = plan(game, remove=name, hw=state.get("hardware_used"))
    apply_plan(game, pl, "revert " + name)
    return True, "Removed %s from %s. The launch options were rebuilt from the remaining profiles." % (name, game)


def all_games():
    """Names of games that have an undo journal (i.e. that gametune has modified)."""
    try:
        return sorted(f[:-5] for f in os.listdir(paths.sub("undo")) if f.endswith(".json"))
    except OSError:
        return []
