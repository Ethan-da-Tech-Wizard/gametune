# ==============================================================================
# Program: cli.py
# Programmer: Ethan-da-Tech-Wizard
# Last Modified: November 18, 2026
# Purpose: Command-line interface argument parsing, JSON serialization, and sub-command routing.
# ==============================================================================

"""Command line. Plain sentences, no colour, every command also supports --json."""

# File role: the only entry point users touch (`python -m gametune ...`, see __main__.py). Each sub-command is a cmd_* function that
# calls hardware, thermal, profiles, report or selftest and returns a process exit code. No game logic lives here.
# Design decisions: every command supports --json for scripts, writes only plain sentences otherwise, and uses distinct exit codes
# (2 = could not conclude, 3 = refused for heat) so wrapper scripts can tell "no answer" from "success" from "failure".
# Known limit: no colour or progress bars on purpose, so output stays readable in logs and screen readers.

import argparse
import json
import sys

from . import __version__, hardware, paths, profiles, report, selftest, thermal

# Shown whenever a profile adds a launch wrapper/overlay: anti-cheat systems
# can treat injected overlays as tampering and ban accounts.
# Kept as a module constant so every command that adds a wrapper prints the exact same warning, and tests can match it.
ANTICHEAT = ("Warning: do not use overlays or launch wrappers with games that use anti-cheat unless the publisher says it is allowed. "
             "gametune never touches game files.")


# One output path for all commands guarantees that --json and text modes cannot drift apart in what they report.
def _out(args, data, lines):
    """Print `data` as JSON when --json is set, otherwise print `lines` as plain text."""
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        for ln in lines:
            print(ln)


def cmd_scan(args):
    """`gametune scan`: detect hardware and helper tools. Always exits 0."""

    # scan() reads only /proc, /sys and a few read-only commands, so it is safe to call from a dry-run command.
    info = hardware.scan()
    _out(args, info, hardware.describe(info))
    return 0


def cmd_thermal(args):
    """`gametune thermal`: read temperatures and report the thermal guard decision.

    Exit status: 0 OK, 2 sensors unreadable (UNKNOWN), 1 any other guard state.
    """

    # Applying only writes text files, so a hot machine does not block it; the message is shown for information.
    s = thermal.sample()
    # The final True asks for a "heavy work" decision, which uses the strictest limits.
    state, msg = thermal.evaluate(s["gpu_c"], s["cpu_c"], True)
    # Sensors may be missing; say so instead of printing "None".
    # Missing sensors are common on laptops and VMs; "not readable" is honest, whereas "0 C" would look like a real reading.
    fmt = lambda v: "%.0f C" % v if v is not None else "not readable"
    lines = ["GPU temperature: %s." % fmt(s["gpu_c"]), "CPU temperature: %s." % fmt(s["cpu_c"]),
             "Guard result: %s. %s" % (state, msg),
             "Limits: heavy work starts at GPU 84 C or lower, pauses at 86 C, stops at 91 C. gametune never allows 97 C and never overclocks."]
    _out(args, {"sample": s, "state": state, "message": msg}, lines)

    # Distinct code for UNKNOWN lets a script tell "too hot" (1) from "could not tell" (2).
    return {thermal.OK: 0, thermal.UNKNOWN: 2}.get(state, 1)


def cmd_selftest(args):
    """`gametune selftest`: run a short headless scene to prove measuring works.

    Exit status: 0 OK, 2 skipped (tools missing), 3 refused/aborted for thermal
    reasons, 1 anything else.
    """

    # Selftest is the one command that starts other programs (vkcube / MangoHud); it is gated by the thermal guard inside selftest.run.
    r = selftest.run(seconds=args.seconds, scene=args.scene, mangohud_path=args.mangohud_path,
                     allow_unknown_temp=args.allow_unknown_temp)
    _out(args, r, r["lines"] + ["RESULT: %s" % r["status"]])

    # All thermal refusals share code 3 so a caller can say "retry when cooler" without parsing text.
    return {"OK": 0, "SKIPPED": 2, "REFUSED_HOT": 3, "REFUSED_UNKNOWN_TEMP": 3, "ABORTED_HOT": 3}.get(r["status"], 1)


def cmd_profiles(args):
    """`gametune profiles`: list profiles with their evidence level.

    "Established" ideas are still unproven for a given PC until `report` says so;
    experimental ones are hidden unless --experimental is passed.
    """
    # Collect both machine-readable and text forms in one pass.
    # Built together in one loop so the JSON and text outputs always describe the same profiles.
    data, lines = [], []
    for p in profiles.list_profiles(args.experimental):
        # Wording is deliberate: even "established" profiles are never claimed to help until measured.
        label = "ESTABLISHED idea, still measure it" if p.evidence == "established" else "UNPROVEN, test it"
        lines.append("%s [%s%s]: %s Options: %s" % (p.name, label, "" if p.default_on else ", off by default", p.summary, p.params))
        data.append({"name": p.name, "evidence": p.evidence, "default_on": p.default_on, "summary": p.summary})
    if not args.experimental:
        lines.append("Unproven profiles are hidden. Show them with: gametune profiles --experimental")
    for name, why in profiles.NOT_OFFERED:
        lines.append("Not offered: %s. %s" % (name, why))
    lines.append("No profile is proven on your PC until `gametune report` says IMPROVED for your game.")
    _out(args, data, lines)
    return 0


def cmd_apply(args):
    """`gametune apply PROFILE --game NAME`: plan, and with --yes write, a profile.

    Without --yes this is a dry run that only prints what would be written.
    Returns 0 on success, 1 when the profile is unknown/refused.
    """
    try:
        if args.profile not in profiles.PROFILES:
            raise profiles.ProfileError("Unknown profile %s. Run: gametune profiles" % args.profile)
        prof = profiles.PROFILES[args.profile]
        # Unproven profiles need an explicit opt-in so nobody applies them by accident.
        # Explicit opt-in: unproven ideas can waste a user's measuring time or hurt performance, so they are never applied by accident.
        if not prof.default_on and not args.experimental:
            raise profiles.ProfileError("%s is unproven and off by default. Add --experimental if you want to test it." % args.profile)
        info = hardware.scan()
        # Hardware facts the planner needs to choose vendor-specific options and CPU pinning.
        # Only the two facts builders actually use are passed, keeping the planner's input small and easy to fake in tests.
        hw = {"gpu_vendor": hardware.primary_gpu_vendor(info), "performance_cpus": info["cpu"]["performance_cpus"]}
        # Only pass options the user actually supplied, so profile defaults still apply.
        params = {k: v for k, v in (("scale", args.scale), ("output", args.output), ("fps", args.fps)) if v is not None}
        # plan() is pure: it computes the file contents without touching the disk.
        # Dry run and real run share this exact plan, so what the user previews is byte-for-byte what --yes writes.
        pl = profiles.plan(args.game, add=(args.profile, params), hw=hw)
        s = thermal.sample()
        # Informational only here (False = not starting heavy work): applying a profile just writes text files.
        state, msg = thermal.evaluate(s["gpu_c"], s["cpu_c"], False)
        lines = ["Thermal check: %s" % msg]
        for p, (data, _m) in sorted(pl["files"].items()):
            lines.append("%s %s" % ("WILL WRITE" if args.yes else "WOULD WRITE (dry run)", p))
        launch = pl["targets"]["launch"]
        if launch in pl["files"]:
            lines.append("Steam launch options would be: " + pl["files"][launch][0].decode().strip())
        lines += pl["notes"]
        # Advice-only profiles add no overlay or wrapper, so the anti-cheat warning is not needed.
        # close-apps writes no launch wrapper or overlay, so the anti-cheat warning would only be noise there.
        if prof.kind != "advice":
            lines.append(ANTICHEAT)
        # Only now does anything get written to disk.
        # --yes is a second, deliberate step: the default is a dry run so nothing is written by a mistyped command.
        if args.yes:
            profiles.apply_plan(args.game, pl, "apply " + args.profile)
            lines.append("Done. Paste the launch options into the game's Steam Launch Options. Undo with: gametune revert %s --game %s" % (args.profile, args.game))
        else:
            lines.append("Dry run: nothing was written. Add --yes to write these files.")
        _out(args, {"dry_run": not args.yes, "files": sorted(pl["files"]), "profiles": pl["profiles"]}, lines)
        return 0

    # Expected user errors print one sentence and return 1; unexpected exceptions still raise so real bugs are not hidden.
    except profiles.ProfileError as exc:
        print("Cannot apply: %s" % exc)
        return 1


def cmd_revert(args):
    """`gametune revert`: undo one profile for a game, or everything with --all.

    Returns 0 only if every revert succeeded.
    """
    # Need either a specific profile+game, or --all.
    # Revert needs enough information to be unambiguous; guessing which game to undo could restore the wrong files.
    if not args.all and not (args.profile and args.game):
        print("Use: gametune revert PROFILE --game NAME   or   gametune revert --all")
        return 1
    try:
        results = []
        if args.all:

            # With --all and no --game, undo every game that has an undo journal, so one command returns the whole machine to its original state.
            games = [args.game] if args.game else profiles.all_games()
            if not games:
                results.append((True, "Nothing to undo."))
            for g in games:
                results.append(profiles.revert_game(g, args.force))
        else:
            results.append(profiles.revert_profile(args.game, args.profile, force=args.force))
    except profiles.ProfileError as exc:
        print("Cannot revert: %s" % exc)
        return 1
    _out(args, [{"ok": ok, "message": m} for ok, m in results], [m for _ok, m in results])

    # One failed restore makes the whole command fail, so scripts notice a partial revert.
    return 0 if all(ok for ok, _ in results) else 1


def cmd_report(args):
    """`gametune report`: compare baseline vs tuned MangoHud logs and save the result.

    Exit status 2 means NOT ENOUGH DATA so scripts can tell "no claim" from success.
    """
    # expand() turns folders into their CSV logs before parsing.
    # min_runs and warmup_s come straight from the CLI so a user can be stricter than the default but the report still refuses fewer than 3 runs.
    rep = report.build(report.expand(args.baseline), report.expand(args.tuned), warmup_s=args.warmup, min_runs=args.runs)
    jpath, tpath = report.save(rep)
    if args.json:
        rep["saved"] = [jpath, tpath]
        print(json.dumps(rep, indent=2))
    else:
        print(report.render_text(rep), end="")
        print("Saved on this computer only: %s and %s" % (jpath, tpath))

    # NOT ENOUGH DATA returns 2 so it can never be mistaken for a successful measurement.
    return 0 if rep["verdict"] != "NOT ENOUGH DATA" else 2


def build_parser():
    """Build the argparse parser with one sub-command per cmd_* function."""

    # prog is fixed so help text reads "gametune" whether run via `python -m` or an installed script.
    ap = argparse.ArgumentParser(prog="gametune", description="Honest game performance helper. Local only. No telemetry. No network.")
    ap.add_argument("--version", action="version", version="gametune " + __version__)

    # dest="cmd" with no `required=True`: running bare `gametune` prints help (handled in main) instead of an argparse error.
    sub = ap.add_subparsers(dest="cmd")
    # Helper: register a sub-command, give it --json, and bind its handler via set_defaults(fn=...).
    # Closure over `sub`; keeps each add() call one line and guarantees every command gets --json.
    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--json", action="store_true", help="machine-readable output")
        p.set_defaults(fn=fn)
        return p
    add("scan", cmd_scan, "detect this PC's hardware and helper tools")
    add("thermal", cmd_thermal, "read temperatures and apply the thermal guard")
    # Per-command options are attached to the parser returned by add().
    p = add("selftest", cmd_selftest, "headless check that measuring works (no real game)")

    # Range 3-20 is enforced inside selftest.run, not by argparse, so the error message can be a plain sentence.
    p.add_argument("--seconds", type=int, default=10, help="scene length, 3 to 20")
    p.add_argument("--scene", choices=["auto", "vkcube", "vkmark"], default="auto")
    p.add_argument("--mangohud-path", help="mangohud program, or an extracted tree containing usr/")
    p.add_argument("--allow-unknown-temp", action="store_true", help="run even if no temperature sensor can be read")
    p = add("profiles", cmd_profiles, "list safe, undoable profiles")
    p.add_argument("--experimental", action="store_true", help="also list unproven profiles")
    p = add("apply", cmd_apply, "write launch options for a profile (dry run unless --yes)")
    p.add_argument("profile")
    p.add_argument("--game", required=True)
    p.add_argument("--yes", action="store_true", help="really write the files")
    p.add_argument("--experimental", action="store_true")

    # Left as strings and validated in profiles.py so messages name the option exactly as the user typed it; None means "use the profile default".
    p.add_argument("--scale"); p.add_argument("--output"); p.add_argument("--fps")
    p = add("revert", cmd_revert, "undo applied profiles exactly")
    p.add_argument("profile", nargs="?")
    p.add_argument("--game"); p.add_argument("--all", action="store_true"); p.add_argument("--force", action="store_true")
    p = add("report", cmd_report, "compare real MangoHud logs before and after")
    p.add_argument("--baseline", nargs="+", required=True, help="baseline log files or folders")
    p.add_argument("--tuned", nargs="+", required=True, help="tuned log files or folders")

    # 3 is the floor the statistics code requires; lower values are rejected there, not here.
    p.add_argument("--runs", type=int, default=3, help="runs needed per side (minimum 3)")
    p.add_argument("--warmup", type=float, default=5.0, help="seconds thrown away at the start of each run")
    return ap


def main(argv=None):
    """Entry point. Returns a process exit code (0 when only help is printed)."""
    ap = build_parser()
    args = ap.parse_args(argv)
    # No sub-command given: show help rather than erroring.
    # getattr because argparse leaves `fn` unset when no sub-command was typed.
    if not getattr(args, "fn", None):
        ap.print_help()
        return 0
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
