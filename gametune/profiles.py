# Program: gametune - profiles.py
# Programmer: Ethan-da-Tech-Wizard (written with Claude, an AI assistant)
# Last Modified: 2026-11-18
# Purpose: Defines the named launch-option profiles (render scale, FPS cap, measuring, shader cache and unproven extras), plans and writes their files with an undo record, and reverts them byte for byte.

"""Profiles: small, named, undoable launch-option recipes.

gametune only ever writes text files under its own data folder. It never edits game files or system files,
never uses sudo, never kills programs. You paste the launch string into Steam yourself (or run the wrapper).

Evidence labels are honest:
  "established"  The mechanism is basic (fewer pixels or fewer frames means less work). Still, nothing is
                 proven on YOUR PC until `gametune report` says IMPROVED for your game.
  "unproven"     Folklore or tiny effect. OFF unless you pass --experimental. Test it with `gametune report`.
"""
# Standard library: base64/hashlib for undo snapshots, json for state files, re for validation, shlex to quote shell text, time for stamps.
import base64
import hashlib
import json
import os
import re
import shlex
import time

# paths gives gametune's data folder; thermal is imported for the shared safety code.
from . import paths, thermal

# Game names may only use safe characters so they can never escape the data folder (no slashes).
GAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ProfileError(Exception):
    """Raised for any user mistake in a profile or game name; the message is a plain sentence."""
    pass


class Piece(object):
    """What one profile adds to the launch line."""

    def __init__(self):
        """Start with empty collections for each kind of addition."""
        self.env = {}
        self.prefix = []        # commands before gamescope (gamemoderun, taskset ...)
        self.gamescope = []     # gamescope arguments
        self.mangohud = []      # MangoHud config options
        self.notes = []


def _even(x):
    """Round to the nearest whole number and make it even."""
    x = int(round(x))
    # Round to a whole number and make it even (video sizes should be even).
    return x - (x % 2)


def _render_scale(params, hw):
    """Build a gamescope render-scale piece from the --scale and --output options."""
    # Default output size is 1920x1080.
    out = params.get("output", "1920x1080")
    # Must look like WIDTHxHEIGHT.
    m = re.match(r"^(\d{3,5})x(\d{3,5})$", out)
    if not m:
        raise ProfileError("--output must look like 1920x1080.")
    # Split the match into width and height numbers.
    W, H = int(m.group(1)), int(m.group(2))
    # Read the scale factor (default 0.75).
    try:
        s = float(params.get("scale", "0.75"))
    except ValueError:
        raise ProfileError("--scale must be a number such as 0.75.")
    # Only allow scales between 0.3 and 1.0 so the picture is never absurd.
    if not 0.3 <= s <= 1.0:
        raise ProfileError("--scale must be between 0.3 and 1.0.")
    p = Piece()
    # Build gamescope arguments: -w/-h is the game size, -W/-H the output size, -F fsr turns on FSR 1 upscaling.
    p.gamescope = ["-w", str(_even(W * s)), "-h", str(_even(H * s)), "-W", str(W), "-H", str(H), "-F", "fsr"]
    # Explain the effect in plain words.
    p.notes.append("The game draws at %dx%d and gamescope scales it up to %dx%d with FSR 1. Sharpness drops a little." % (_even(W * s), _even(H * s), W, H))
    return p


def _fps_cap(params, hw):
    """Build a MangoHud frame-rate cap piece from the --fps option."""
    # Read the FPS cap (default 60).
    try:
        n = int(params.get("fps", "60"))
    except ValueError:
        raise ProfileError("--fps must be a whole number such as 60.")
    # Sensible range only.
    if not 20 <= n <= 1000:
        raise ProfileError("--fps must be between 20 and 1000.")
    p = Piece()
    # MangoHud option that limits frame rate.
    p.mangohud = ["fps_limit=%d" % n]
    p.notes.append("Caps the game at %d FPS using MangoHud. A cap mostly makes frame times steadier and the PC cooler; it does not raise FPS." % n)
    return p


def _measure(params, hw):
    """Build the MangoHud logging piece used to record frame times."""
    p = Piece()
    # Log into gametune's own folder unless the caller chose another.
    folder = params.get("log_folder") or paths.sub("logs")
    # MangoHud options: where to write, when to start logging, how long, and interval 0 meaning every frame.
    p.mangohud = ["output_folder=" + folder, "autostart_log=%s" % params.get("delay", "5"),
                  "log_duration=%s" % params.get("seconds", "90"), "log_interval=0"]
    p.notes.append("MangoHud writes a frame-time log to %s. Use at least 3 runs per side with `gametune report`." % folder)
    return p


def _shader_cache(params, hw):
    """Build the shader-cache environment piece for the detected GPU vendor."""
    p = Piece()
    # NVIDIA and Mesa drivers each use their own environment variable for the shader cache.
    vendor = (hw or {}).get("gpu_vendor")
    if vendor == "NVIDIA":
        p.env["__GL_SHADER_DISK_CACHE_SKIP_CLEANUP"] = "1"
        p.notes.append("NVIDIA: stops the driver from trimming its shader cache, which can reduce repeated stutter. It does not raise average FPS.")
    else:
        p.env["MESA_SHADER_CACHE_MAX_SIZE"] = "10G"
        p.notes.append("Mesa (AMD/Intel): lets the shader cache grow to 10 GB so it is not trimmed. It does not raise average FPS.")
    return p


def _dxvk_async(params, hw):
    """Build the (unproven) DXVK_ASYNC piece."""
    p = Piece()
    p.env["DXVK_ASYNC"] = "1"
    p.notes.append("Unproven. It belongs to a patched DXVK and may do nothing on current Proton. Measure before keeping it.")
    return p


def _gamemode(params, hw):
    """Build the (unproven) GameMode piece."""
    p = Piece()
    # gamemoderun is a command placed in front of the game.
    p.prefix = ["gamemoderun"]
    p.notes.append("Unproven. Published gains are small and often inside normal noise. Needs the gamemode package.")
    return p


def _core_pinning(params, hw):
    """Build the (unproven) taskset core-pinning piece; refuses when no hybrid CPU was detected."""
    p = Piece()
    # Needs the list of fast cores found by hardware.py.
    perf = (hw or {}).get("performance_cpus")
    # No hybrid CPU found: refuse instead of guessing.
    if not perf:
        raise ProfileError("core-pinning needs a hybrid CPU that `gametune scan` can see (performance and efficiency cores). "
                           "None was found on this PC, so nothing is written.")
    # taskset -c <cpus> runs the game only on those CPUs.
    p.prefix = ["taskset", "-c", perf]
    p.notes.append("Unproven. Pins the game to the performance threads (%s). Modern kernels usually schedule hybrid CPUs well; this can even hurt." % perf)
    return p


class Profile(object):
    """One named profile: description, evidence label, builder function and whether it is on by default."""
    def __init__(self, name, summary, evidence, builder, kind="launch", default_on=True, params=""):
        """Save the profile's fields on the object."""
        # Store the name, description, evidence label, builder function and options text.
        self.name, self.summary, self.evidence, self.builder = name, summary, evidence, builder
        self.kind, self.default_on, self.params = kind, default_on, params


# Registry of every profile, keyed by name. A builder of None means the profile only gives advice.
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

# Ideas gametune refuses to offer, with the reason shown to the user.
NOT_OFFERED = [("nice / process priority", "Raising priority needs administrator rights. gametune never uses sudo, so it is not offered.")]

# Program names (lowercase) that the advice profile treats as memory heavy.
HEAVY_APPS = ("firefox", "chrome", "chromium", "brave", "vivaldi", "discord", "slack", "teams", "spotify", "obs", "thunderbird", "zoom", "code")


def list_profiles(experimental=False):
    """Return the profiles to show."""
    # Default-on profiles always; unproven ones only when asked.
    return [p for p in PROFILES.values() if p.default_on or experimental]


def running_heavy_apps(root="/"):
    """Read-only look at /proc for well-known memory-hungry programs. Returns [(name, rss_mib)]."""
    # Look at the /proc folder (a fake root is allowed in tests).
    found = {}
    proc = os.path.join(root, "proc")
    # Every folder whose name is all digits is a process id.
    try:
        entries = [e for e in os.listdir(proc) if e.isdigit()]
    except OSError:
        return []
    # Memory pages are counted in 4096-byte units; convert to MiB.
    page_mib = 4096.0 / (1024 * 1024)
    for pid in entries:
        # Read the program name and see if it is in the heavy list.
        try:
            with open(os.path.join(proc, pid, "comm")) as f:
                comm = f.read().strip().lower()
            if not any(h == comm or comm.startswith(h) for h in HEAVY_APPS):
                continue
            # statm's second number is resident pages; multiply by the page size.
            with open(os.path.join(proc, pid, "statm")) as f:
                rss = int(f.read().split()[1]) * page_mib
        # Processes that vanish or have odd files are skipped.
        except (OSError, ValueError, IndexError):
            continue
        # Add up memory per program name.
        found[comm] = found.get(comm, 0.0) + rss
    # Biggest first, rounded to whole MiB.
    return sorted(((k, round(v)) for k, v in found.items()), key=lambda kv: -kv[1])


def check_game(name):
    """Return the game name if it is safe, otherwise raise ProfileError."""
    # Reject names with other characters.
    if not GAME_RE.match(name or ""):
        raise ProfileError("Game name may only use letters, digits, dot, dash and underscore (max 64), and must start with a letter or digit.")
    return name


def compose(selected, hw):
    """selected: {profile name: params}. Returns (launch_options string, wrapper script text, notes)."""
    # Collect what every selected profile adds to the launch line.
    env, prefix, gs, mh, notes = {}, [], [], [], []
    # sorted() keeps the result in the same order every time.
    for name in sorted(selected):
        prof = PROFILES[name]
        # Advice-only profiles add nothing to the command.
        if prof.builder is None:
            continue
        # Call the profile's builder with the user's options and the hardware facts.
        piece = prof.builder(selected[name], hw)
        env.update(piece.env)
        prefix += piece.prefix
        gs += piece.gamescope
        mh += piece.mangohud
        notes += piece.notes
    # All MangoHud options travel in one MANGOHUD_CONFIG variable.
    if mh:
        env["MANGOHUD_CONFIG"] = ",".join(mh)
    # Build the Steam launch line: ENV=value ... prefix gamescope ... -- mangohud %command%.
    parts = []
    for k in sorted(env):
        # shlex.quote makes values safe to paste into a shell.
        parts.append("%s=%s" % (k, shlex.quote(env[k])))
    parts += [shlex.quote(x) for x in prefix]
    if gs:
        parts += ["gamescope"] + [shlex.quote(x) for x in gs] + ["--"]
    if mh:
        parts.append("mangohud")
    # %command% is Steam's placeholder for the game itself.
    steam = " ".join(parts + ["%command%"])
    # Build a small wrapper script with the same pieces.
    wrap_env = ["%s=%s" % (k, shlex.quote(env[k])) for k in sorted(env)]
    wparts = (["env"] + wrap_env if wrap_env else []) + [shlex.quote(x) for x in prefix]
    if gs:
        wparts += ["gamescope"] + [shlex.quote(x) for x in gs] + ["--"]
    if mh:
        wparts.append("mangohud")
    # The script starts with #!/bin/sh and ends with exec, passing the game's arguments through as "$@".
    wrapper = "#!/bin/sh\n# Written by gametune. Undo with: gametune revert --all\nexec %s\n" % " ".join(wparts + ['"$@"'])
    # Return the Steam line, the wrapper text and the notes.
    return steam + "\n", wrapper, notes


def _sha(data):
    """Return the SHA-256 hex digest of some bytes."""
    # SHA-256 fingerprint of some bytes, used to detect later edits.
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path):
    """Return a file's bytes, or None if it cannot be read."""
    # Read a whole file as bytes; None when it does not exist.
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _targets(game):
    """Return the dictionary of file paths gametune uses for a game."""
    # Every file gametune may write for one game, all inside its own data folder.
    return {
        "launch": paths.sub("launch", game + ".launch.txt"),
        "wrapper": paths.sub("wrappers", game + ".sh"),
        "state": paths.sub("state", game + ".json"),
        "notes": paths.sub("advice", game + ".txt"),
    }


def _undo_path(game):
    """Return the path of a game's undo history file."""
    # Where the undo history for a game lives.
    return paths.sub("undo", game + ".json")


def _write_atomic(path, data, mode=None):
    """Write bytes to a file atomically (temporary file then rename), optionally setting permissions."""
    # Make the folder if needed.
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Write to a temporary file first, then rename: the real file is never left half written.
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    # Optional permissions, e.g. 0o755 for the wrapper script.
    if mode is not None:
        os.chmod(tmp, mode)
    # os.replace swaps the new file in in one step.
    os.replace(tmp, path)


def load_state(game):
    """Load the saved profile state for a game (an empty one if none)."""
    # Saved state lists which profiles are applied to the game.
    raw = _read_bytes(_targets(game)["state"])
    if raw is None:
        # No state yet: nothing applied.
        return {"game": game, "profiles": {}}
    return json.loads(raw.decode("utf-8"))


def plan(game, add=None, remove=None, hw=None, root="/"):
    """Work out the files that would be written. Pure; writes nothing. Returns dict path -> (bytes, mode) and summary."""
    # Validate the game name first.
    check_game(game)
    # Start from what is already applied, then add or remove one profile.
    state = load_state(game)
    profiles = dict(state.get("profiles", {}))
    if add:
        profiles[add[0]] = add[1]
    if remove:
        profiles.pop(remove, None)
    # Work out every target path.
    t = _targets(game)
    files = {}
    notes = []
    # If any profiles remain, build all the files.
    if profiles:
        steam, wrapper, notes = compose(profiles, hw)
        # Launch text for Steam (normal file permissions).
        files[t["launch"]] = (steam.encode("utf-8"), 0o644)
        # Wrapper script (executable).
        files[t["wrapper"]] = (wrapper.encode("utf-8"), 0o755)
        # close-apps also writes an advice list of heavy programs; nothing is closed.
        if "close-apps" in profiles:
            apps = running_heavy_apps(root)
            body = "Close these before playing (gametune never closes anything for you):\n" + (
                "".join("  %s  about %d MiB\n" % a for a in apps) if apps else "  Nothing well-known was running.\n")
            files[t["notes"]] = (body.encode("utf-8"), 0o644)
        # Save the applied profiles and the hardware facts used, so a later revert can rebuild the launch line.
        state_out = {"game": game, "profiles": profiles, "hardware_used": {k: v for k, v in (hw or {}).items() if v}}
        files[t["state"]] = ((json.dumps(state_out, indent=2, sort_keys=True) + "\n").encode("utf-8"), 0o644)
    # Return the plan only; nothing has been written.
    return {"files": files, "profiles": profiles, "notes": notes, "targets": t}


def apply_plan(game, pl, label):
    """Snapshot every file we are about to touch, then write. Records an undo entry first."""
    # `t` holds the target paths for this game.
    t = pl["targets"]
    all_paths = list(t.values())
    # Load the existing undo history, or start a new one.
    undo = json.loads(_read_bytes(_undo_path(game)).decode("utf-8")) if _read_bytes(_undo_path(game)) else {"game": game, "records": []}
    # Snapshot every file that might change, as base64 text (None when the file does not exist).
    snap = {}
    for p in all_paths:
        data = _read_bytes(p)
        snap[p] = None if data is None else base64.b64encode(data).decode("ascii")
    # Remember a fingerprint of what we are about to write.
    after = {p: _sha(d) for p, (d, _m) in pl["files"].items()}
    # Add a record: time, label, the before-snapshot and the after-fingerprints.
    undo["records"].append({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "label": label, "before": snap, "after_sha256": after})
    # (Undo record is written before any real file changes.)
    _write_atomic(_undo_path(game), (json.dumps(undo, indent=2) + "\n").encode("utf-8"))   # undo record first
    # Now write each planned file.
    for p in all_paths:
        if p in pl["files"]:
            data, mode = pl["files"][p]
            _write_atomic(p, data, mode)
        # A file this plan no longer produces is removed (only files gametune created).
        elif os.path.exists(p):
            os.remove(p)       # a file gametune made earlier that this profile set no longer needs


def revert_game(game, force=False):
    """Restore the exact bytes (or absence) of every file as they were before the first apply."""
    # Validate the game name.
    check_game(game)
    up = _undo_path(game)
    raw = _read_bytes(up)
    # No undo history means gametune never changed this game.
    if raw is None:
        return False, "Nothing to undo for %s." % game
    # Load the history.
    undo = json.loads(raw.decode("utf-8"))
    # The newest record shows what we last wrote.
    last = undo["records"][-1] if undo["records"] else None
    # Unless --force, refuse if the files were edited since gametune wrote them.
    if last and not force:
        for p, digest in last["after_sha256"].items():
            cur = _read_bytes(p)
            if cur is None or _sha(cur) != digest:
                return False, "%s changed after gametune wrote it. Nothing was restored. Use --force to restore anyway." % p
    # The first record holds the very first (original) state of each file.
    first = undo["records"][0]["before"] if undo["records"] else {}
    # Put every file back exactly as it was.
    for p, b64 in first.items():
        if b64 is None:
            # It did not exist originally, so delete it.
            if os.path.exists(p):
                os.remove(p)
        else:
            _write_atomic(p, base64.b64decode(b64))
    # History is no longer needed once everything is restored.
    os.remove(up)
    return True, "Restored %s to exactly how it was before gametune touched it." % game


def revert_profile(game, name, hw=None, force=False):
    """Remove one profile from a game and rebuild its files (or fully revert if it was the only one)."""
    # Revert reads the current state of the game.
    state = load_state(game)
    if name not in state.get("profiles", {}):
        # That profile was never applied.
        return False, "Profile %s is not applied to %s." % (name, game)
    # If it is the only profile, a full revert restores the original files.
    if len(state["profiles"]) == 1:
        return revert_game(game, force)
    # Otherwise rebuild the files without that profile.
    pl = plan(game, remove=name, hw=state.get("hardware_used"))
    # Applying the new plan also records an undo entry.
    apply_plan(game, pl, "revert " + name)
    return True, "Removed %s from %s. The launch options were rebuilt from the remaining profiles." % (name, game)


def all_games():
    """List every game that has an undo history."""
    # Each undo file is named <game>.json; strip the 5-character '.json'.
    try:
        return sorted(f[:-5] for f in os.listdir(paths.sub("undo")) if f.endswith(".json"))
    except OSError:
        return []
