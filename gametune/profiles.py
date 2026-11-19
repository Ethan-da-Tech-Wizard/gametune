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

# File role: the heart of gametune. cli.py calls list_profiles / plan / apply_plan / revert_* ; hardware.py supplies the `hw` dict
# (gpu_vendor, performance_cpus) that builders read; paths.py decides where files live. Nothing here launches a game or
# touches Steam: the output is text files (launch line, wrapper script, state, undo journal) the user chooses to use.
# Key design decision: every write is preceded by an undo-journal entry holding the exact previous bytes, so `revert` can
# restore the machine to byte-for-byte how it was. Known limit: profiles for several games are independent; there is no global lock.
# Test: `python3 tests/run_tests.py` (tests/test_gametune.py exercises compose, plan, apply and revert in a temp HOME).

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
# Max 64 chars keeps file names well under common filesystem limits even after the ".launch.txt" suffix is added.
GAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


# Raised for every expected input problem, so cli.py can catch one type, print the message and exit non-zero without a traceback.
class ProfileError(Exception):
    """User-facing error: the message is a plain sentence printed by the CLI."""
    pass


class Piece(object):
    """What one profile adds to the launch line."""

    def __init__(self):
        # Environment variables to set (name -> value).
        # Four separate lists (not one command string) because compose() must emit them in a fixed order that Steam understands.
        self.env = {}
        self.prefix = []        # commands before gamescope (gamemoderun, taskset ...)
        self.gamescope = []     # gamescope arguments
        self.mangohud = []      # MangoHud config options
        self.notes = []


def _even(x):
    """Round to the nearest integer, then down to an even number.

    Scaled resolutions are kept even because some video paths reject odd sizes.
    """

    # `x - (x % 2)` rounds down so the result is even; 1440 stays 1440 and an odd 1081 becomes 1080.
    x = int(round(x))
    return x - (x % 2)


def _render_scale(params, hw):
    """Builder for "render-scale": gamescope draws small and upscales with FSR 1.

    Validates --output (WxH) and --scale (0.3 to 1.0) before building anything.
    """

    # Default is the most common display; users on other panels pass --output.
    out = params.get("output", "1920x1080")
    # Accept only WxH with 3-5 digit sizes; anything else could not be a real display mode.
    m = re.match(r"^(\d{3,5})x(\d{3,5})$", out)
    if not m:
        raise ProfileError("--output must look like 1920x1080.")
    W, H = int(m.group(1)), int(m.group(2))
    try:
        # Default 0.75: a 25% cut in each dimension, a common balance of speed and sharpness.
        s = float(params.get("scale", "0.75"))
    except ValueError:
        raise ProfileError("--scale must be a number such as 0.75.")
    # Below 0.3 the image is unusable; above 1.0 would be supersampling, not a speed-up.
    if not 0.3 <= s <= 1.0:
        raise ProfileError("--scale must be between 0.3 and 1.0.")

    # Each builder creates its own Piece and fills only the parts it needs; compose() merges them.
    p = Piece()
    # gamescope flags: -w/-h = game (internal) size, -W/-H = output size, -F fsr = FSR 1 upscaler.
    p.gamescope = ["-w", str(_even(W * s)), "-h", str(_even(H * s)), "-W", str(W), "-H", str(H), "-F", "fsr"]
    p.notes.append("The game draws at %dx%d and gamescope scales it up to %dx%d with FSR 1. Sharpness drops a little." % (_even(W * s), _even(H * s), W, H))
    return p


def _fps_cap(params, hw):
    """Builder for "fps-cap": a MangoHud fps_limit between 20 and 1000."""
    try:

        # int() rejects "60.5" on purpose: fps_limit takes whole numbers in MangoHud.
        n = int(params.get("fps", "60"))
    except ValueError:
        raise ProfileError("--fps must be a whole number such as 60.")

    # Below 20 the game feels broken; above 1000 is no limit at all on any real display.
    if not 20 <= n <= 1000:
        raise ProfileError("--fps must be between 20 and 1000.")
    p = Piece()

    # fps_limit is a MangoHud config key; it goes through MANGOHUD_CONFIG (see compose) rather than a command-line flag.
    p.mangohud = ["fps_limit=%d" % n]
    p.notes.append("Caps the game at %d FPS using MangoHud. A cap mostly makes frame times steadier and the PC cooler; it does not raise FPS." % n)
    return p


def _measure(params, hw):
    """Builder for "measure": configure MangoHud to auto-log frame times.

    autostart_log is the delay in seconds before logging begins, log_duration
    the length, and log_interval=0 means one row per frame (not sampled).
    """
    p = Piece()
    # Logs go inside gametune's own data folder unless told otherwise.
    # paths.sub keeps logs inside gametune's data folder so `gametune report` knows where to look and `revert` never touches user directories.
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
    # `hw` may be None when no scan was done; (hw or {}) keeps that case safe.
    vendor = (hw or {}).get("gpu_vendor")
    if vendor == "NVIDIA":

        # NVIDIA's driver deletes old shader cache entries once the cache reaches a size limit; this variable turns that cleanup off.
        p.env["__GL_SHADER_DISK_CACHE_SKIP_CLEANUP"] = "1"
        p.notes.append("NVIDIA: stops the driver from trimming its shader cache, which can reduce repeated stutter. It does not raise average FPS.")
    else:

        # Mesa's default cap is small enough that large games can evict entries and recompile (stutter); 10G is generous but only uses disk.
        p.env["MESA_SHADER_CACHE_MAX_SIZE"] = "10G"
        p.notes.append("Mesa (AMD/Intel): lets the shader cache grow to 10 GB so it is not trimmed. It does not raise average FPS.")
    return p


def _dxvk_async(params, hw):
    """Builder for the unproven "dxvk-async" profile (DXVK_ASYNC=1)."""
    p = Piece()

    # Only patched DXVK builds read this variable, hence the "unproven" label and default_on=False.
    p.env["DXVK_ASYNC"] = "1"
    p.notes.append("Unproven. It belongs to a patched DXVK and may do nothing on current Proton. Measure before keeping it.")
    return p


def _gamemode(params, hw):
    """Builder for the unproven "gamemode" profile: prefixes the launch with gamemoderun."""
    p = Piece()

    # gamemoderun is a prefix command (it must come before the game), so it goes in prefix rather than env.
    p.prefix = ["gamemoderun"]
    p.notes.append("Unproven. Published gains are small and often inside normal noise. Needs the gamemode package.")
    return p


def _core_pinning(params, hw):
    """Builder for the unproven "core-pinning" profile: taskset onto performance cores.

    Refuses (ProfileError) when no hybrid CPU was detected, so it never writes a meaningless taskset line.
    """
    p = Piece()

    # Format is a taskset CPU list such as "0-15", produced by hardware.py from the CPU's core types.
    perf = (hw or {}).get("performance_cpus")
    if not perf:

        # Refuse rather than write a no-op: a profile the user thinks is active but does nothing would corrupt their later measurements.
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

        # Plain attribute bag; kept as a class (not a namedtuple) so a future profile could add optional fields without breaking the registry.
        self.name, self.summary, self.evidence, self.builder = name, summary, evidence, builder
        self.kind, self.default_on, self.params = kind, default_on, params


# Registry of every profile, keyed by name. The list order is not significant.
# Dict comprehension over a list so each name appears exactly once in the source and is also the dict key.
PROFILES = {p.name: p for p in [
    Profile("render-scale", "Draw the game at a lower resolution and scale it up (gamescope + FSR 1). Fewer pixels means less GPU work.",
            "established", _render_scale, params="--scale 0.75 --output 1920x1080"),
    Profile("fps-cap", "Cap the frame rate (MangoHud). Steadier frame times and a cooler PC. Does not raise FPS.",
            "established", _fps_cap, params="--fps 60"),
    Profile("measure", "Record frame times with MangoHud so `gametune report` has real data.",
            "established", _measure, kind="tool", params="(none)"),
    Profile("shader-cache", "Keep the driver's shader cache from being trimmed. Helps repeat stutter only.",
            "established", _shader_cache, params="(none)"),

    # builder=None marks advice-only profiles: compose() skips them, plan() writes the advice file instead.
    Profile("close-apps", "ADVICE ONLY: lists heavy programs that are running so YOU can close them. Nothing is closed for you.",
            "established", None, kind="advice", params="(none)"),
    Profile("dxvk-async", "Set DXVK_ASYNC=1.", "unproven", _dxvk_async, default_on=False),
    Profile("gamemode", "Run the game through GameMode.", "unproven", _gamemode, default_on=False),
    Profile("core-pinning", "Pin the game to the performance cores of a hybrid CPU.", "unproven", _core_pinning, default_on=False),
]}

# Ideas deliberately left out, with the reason; shown by `gametune profiles`.
# Listing what is NOT offered, with reasons, tells users up front which options the no-sudo rule rules out.
NOT_OFFERED = [("nice / process priority", "Raising priority needs administrator rights. gametune never uses sudo, so it is not offered.")]

# Process-name prefixes of common memory-hungry programs for the advice-only close-apps profile.
# Prefix match (startswith) is used against /proc comm names, which Linux truncates to 15 characters.
HEAVY_APPS = ("firefox", "chrome", "chromium", "brave", "vivaldi", "discord", "slack", "teams", "spotify", "obs", "thunderbird", "zoom", "code")


def list_profiles(experimental=False):
    """Profiles to show: default-on ones, plus unproven ones if `experimental`."""
    return [p for p in PROFILES.values() if p.default_on or experimental]


def running_heavy_apps(root="/"):
    """Read-only look at /proc for well-known memory-hungry programs. Returns [(name, rss_mib)]."""
    # Name -> total resident MiB, summed over all processes sharing that name (browsers run many).
    found = {}

    # `root` is a parameter (not hardcoded "/") so tests can point at a fake /proc tree.
    proc = os.path.join(root, "proc")
    try:
        # Numeric /proc entries are process IDs.
        entries = [e for e in os.listdir(proc) if e.isdigit()]
    except OSError:
        return []
    # /proc/PID/statm counts pages; assumes the usual 4 KiB page size.
    page_mib = 4096.0 / (1024 * 1024)

    # Each process is read independently inside try/except: /proc is racy and one vanished PID must not abort the whole listing.
    for pid in entries:
        try:
            with open(os.path.join(proc, pid, "comm")) as f:
                comm = f.read().strip().lower()
            # Prefix match covers names such as "firefox-bin" and "chrome-sandbox".
            if not any(h == comm or comm.startswith(h) for h in HEAVY_APPS):
                continue
            # Second statm field is resident set size in pages.
            with open(os.path.join(proc, pid, "statm")) as f:
                rss = int(f.read().split()[1]) * page_mib
        # Processes can exit mid-scan; just skip any that vanish or have odd contents.
        except (OSError, ValueError, IndexError):
            continue

        # Summed per name because a browser is dozens of processes; the user cares about the total of each program.
        found[comm] = found.get(comm, 0.0) + rss

    # Largest first so the advice file leads with the programs that matter most.
    return sorted(((k, round(v)) for k, v in found.items()), key=lambda kv: -kv[1])


def check_game(name):
    """Validate a game name against GAME_RE and return it, or raise ProfileError."""

    # `name or ""` handles None so a missing argument gives the same friendly error rather than a TypeError.
    if not GAME_RE.match(name or ""):
        raise ProfileError("Game name may only use letters, digits, dot, dash and underscore (max 64), and must start with a letter or digit.")
    return name


def compose(selected, hw):
    """selected: {profile name: params}. Returns (launch_options string, wrapper script text, notes)."""
    # Accumulators: environment variables, command prefixes, gamescope args, MangoHud options, user notes.
    env, prefix, gs, mh, notes = {}, [], [], [], []
    # Sorted so the generated line is deterministic regardless of dict order.
    # Alphabetical iteration is what makes "later profile wins" predictable (see the update calls below).
    for name in sorted(selected):
        prof = PROFILES[name]
        # Advice-only profiles (close-apps) contribute nothing to the launch line.
        if prof.builder is None:
            continue
        piece = prof.builder(selected[name], hw)
        # Merge each piece; later profiles (alphabetical) override on a conflict.
        env.update(piece.env)
        prefix += piece.prefix
        gs += piece.gamescope
        mh += piece.mangohud
        notes += piece.notes
    # MangoHud takes all options through one MANGOHUD_CONFIG variable.
    # Only add the mangohud wrapper when some profile set an option; otherwise the game would start an overlay nobody asked for.
    if mh:
        env["MANGOHUD_CONFIG"] = ",".join(mh)
    # Steam launch string order: ENV=values, command prefixes, gamescope ... --, mangohud, %command%.
    parts = []
    for k in sorted(env):
        parts.append("%s=%s" % (k, shlex.quote(env[k])))
    parts += [shlex.quote(x) for x in prefix]
    if gs:
        parts += ["gamescope"] + [shlex.quote(x) for x in gs] + ["--"]
    if mh:
        parts.append("mangohud")
    # %command% is Steam's placeholder for the game's own launch command.
    # Steam launch options are passed to /bin/sh by Steam, so shlex.quote protects values containing spaces, such as folder names.
    steam = " ".join(parts + ["%command%"])
    # Same recipe again as a standalone shell wrapper (for non-Steam use); every value is shlex-quoted.
    wrap_env = ["%s=%s" % (k, shlex.quote(env[k])) for k in sorted(env)]
    wparts = (["env"] + wrap_env if wrap_env else []) + [shlex.quote(x) for x in prefix]
    if gs:
        wparts += ["gamescope"] + [shlex.quote(x) for x in gs] + ["--"]
    if mh:
        wparts.append("mangohud")

    # `"$@"` forwards the game command line that the user gives the wrapper, the shell equivalent of %command%.
    wrapper = "#!/bin/sh\n# Written by gametune. Undo with: gametune revert --all\nexec %s\n" % " ".join(wparts + ['"$@"'])
    return steam + "\n", wrapper, notes


# Content hash (not mtime) is used because an editor that rewrites identical content is not a user change.
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


# Four fixed names per game means revert and plan agree on the full set without scanning the folder.
def _targets(game):
    """The four files gametune manages for a game (launch line, wrapper, state, advice)."""
    return {
        # Steam launch options text (user pastes it into Steam).
        "launch": paths.sub("launch", game + ".launch.txt"),
        # Executable wrapper script, for use outside Steam.
        "wrapper": paths.sub("wrappers", game + ".sh"),
        # Which profiles are applied, so later changes can rebuild everything.
        "state": paths.sub("state", game + ".json"),
        # Advice file written by close-apps.
        "notes": paths.sub("advice", game + ".txt"),
    }


def _undo_path(game):
    """Location of the per-game undo journal."""
    return paths.sub("undo", game + ".json")


def _write_atomic(path, data, mode=None):
    """Write via temp file + os.replace so a crash never leaves a half-written file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # The temp file lives next to the target: os.replace is only atomic within one filesystem.
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)

    # 0o755 for the wrapper makes it executable; chmod is done before the rename so the final file is never briefly non-executable.
    if mode is not None:
        os.chmod(tmp, mode)
    # Atomic rename on the same filesystem: readers see the old or the new file, never a mix.
    os.replace(tmp, path)


def load_state(game):
    """Return the saved set of applied profiles for a game (empty if none)."""

    # Missing state is normal for a game gametune has never touched, so it is not an error.
    raw = _read_bytes(_targets(game)["state"])
    if raw is None:
        return {"game": game, "profiles": {}}
    return json.loads(raw.decode("utf-8"))


def plan(game, add=None, remove=None, hw=None, root="/"):
    """Work out the files that would be written. Pure; writes nothing. Returns dict path -> (bytes, mode) and summary."""
    # Validate the name first: it is used to build paths.
    check_game(game)

    # State is the source of truth for "what is applied now"; plan() layers the requested change onto it.
    state = load_state(game)
    # Copy the saved profile set, then apply the requested add/remove to the copy.
    profiles = dict(state.get("profiles", {}))
    if add:
        profiles[add[0]] = add[1]
    if remove:
        profiles.pop(remove, None)
    t = _targets(game)
    # path -> (bytes, file mode) for everything that would be written.
    files = {}
    notes = []
    # With no profiles left, no files are planned; apply_plan then deletes the old ones.
    # Writing files only when profiles remain is what makes removing the last profile delete everything it created.
    if profiles:
        steam, wrapper, notes = compose(profiles, hw)
        files[t["launch"]] = (steam.encode("utf-8"), 0o644)
        files[t["wrapper"]] = (wrapper.encode("utf-8"), 0o755)
        if "close-apps" in profiles:
            apps = running_heavy_apps(root)
            body = "Close these before playing (gametune never closes anything for you):\n" + (
                "".join("  %s  about %d MiB\n" % a for a in apps) if apps else "  Nothing well-known was running.\n")
            files[t["notes"]] = (body.encode("utf-8"), 0o644)
        # Remember the hardware facts used so a later single-profile revert can rebuild identically.
        state_out = {"game": game, "profiles": profiles, "hardware_used": {k: v for k, v in (hw or {}).items() if v}}
        files[t["state"]] = ((json.dumps(state_out, indent=2, sort_keys=True) + "\n").encode("utf-8"), 0o644)
    return {"files": files, "profiles": profiles, "notes": notes, "targets": t}


def apply_plan(game, pl, label):
    """Snapshot every file we are about to touch, then write. Records an undo entry first."""
    t = pl["targets"]
    # Every managed path, not only those being written, so files that go away are snapshotted too.
    all_paths = list(t.values())
    # Load the existing undo journal, or start a new one.
    undo = json.loads(_read_bytes(_undo_path(game)).decode("utf-8")) if _read_bytes(_undo_path(game)) else {"game": game, "records": []}
    # Snapshot (base64 of the exact bytes, or None for "did not exist") of every managed file before writing.
    # Base64 keeps arbitrary bytes safe inside the JSON journal.
    snap = {}
    for p in all_paths:
        data = _read_bytes(p)
        snap[p] = None if data is None else base64.b64encode(data).decode("ascii")
    # Hashes of what we write; revert later compares to detect user edits.
    after = {p: _sha(d) for p, (d, _m) in pl["files"].items()}
    undo["records"].append({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "label": label, "before": snap, "after_sha256": after})

    # The journal is written BEFORE any managed file changes: if the process dies midway, `revert` can still restore the originals.
    _write_atomic(_undo_path(game), (json.dumps(undo, indent=2) + "\n").encode("utf-8"))   # undo record first
    for p in all_paths:
        if p in pl["files"]:
            data, mode = pl["files"][p]
            _write_atomic(p, data, mode)

        # Reaching here means the new plan does not include this file, so it is stale.
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
    # Only the latest record's hashes are checked: they describe what is on disk right now, not older states.
    last = undo["records"][-1] if undo["records"] else None
    if last and not force:
        for p, digest in last["after_sha256"].items():
            cur = _read_bytes(p)
            if cur is None or _sha(cur) != digest:
                return False, "%s changed after gametune wrote it. Nothing was restored. Use --force to restore anyway." % p
    # Restore the state from before the FIRST apply, which undoes every later apply at once.
    # Using the first record, not the last, makes revert a full reset to the original machine state even after many applies.
    first = undo["records"][0]["before"] if undo["records"] else {}
    for p, b64 in first.items():
        # None records that the file did not exist before gametune; undoing means deleting it.
        if b64 is None:
            if os.path.exists(p):
                os.remove(p)
        else:
            _write_atomic(p, base64.b64decode(b64))
    # The journal is deleted last, after everything else was restored.
    os.remove(up)
    return True, "Restored %s to exactly how it was before gametune touched it." % game


def revert_profile(game, name, hw=None, force=False):
    """Remove one profile; if it was the last, restore everything via revert_game."""
    state = load_state(game)
    if name not in state.get("profiles", {}):
        return False, "Profile %s is not applied to %s." % (name, game)

    # Removing the last profile is the same as a full revert, which also restores files that existed before gametune.
    if len(state["profiles"]) == 1:
        return revert_game(game, force)

    # hardware_used comes from the saved state so the rebuild is identical even if the user's hardware scan changed since.
    pl = plan(game, remove=name, hw=state.get("hardware_used"))
    apply_plan(game, pl, "revert " + name)
    return True, "Removed %s from %s. The launch options were rebuilt from the remaining profiles." % (name, game)


def all_games():
    """Names of games that have an undo journal (i.e. that gametune has modified)."""
    try:

        # `[:-5]` strips ".json"; the undo folder only contains journals.
        return sorted(f[:-5] for f in os.listdir(paths.sub("undo")) if f.endswith(".json"))
    except OSError:
        return []
