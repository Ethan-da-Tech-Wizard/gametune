# Program: gametune - cli.py
# Programmer: Ethan-da-Tech-Wizard (written with Claude, an AI assistant)
# Last Modified: 2026-11-18
# Purpose: Defines the gametune command line: argument parsing, one cmd_ function per sub-command, text or --json output, and exit codes.

"""Command line. Plain sentences, no colour, every command also supports --json."""
# Standard library modules: argparse reads the command line, json prints machine-readable output.
import argparse
import json
import sys

# Our own modules (the dot means this package); each one does one job, e.g. hardware scanning or reports.
from . import __version__, hardware, paths, profiles, report, selftest, thermal

# A safety message printed whenever a profile adds launch wrappers.
ANTICHEAT = ("Warning: do not use overlays or launch wrappers with games that use anti-cheat unless the publisher says it is allowed. "
             "gametune never touches game files.")


def _out(args, data, lines):
    """Print `lines` as plain text, or `data` as JSON when --json was given."""
    # --json asked for: print the data as indented JSON (default=str turns odd values into text).
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    # Otherwise print the plain-English lines, one per row.
    else:
        for ln in lines:
            print(ln)


def cmd_scan(args):
    """scan: detect hardware and print it. Returns exit code 0."""
    # Ask hardware.py to look at this PC, then print it.
    info = hardware.scan()
    _out(args, info, hardware.describe(info))
    # 0 means success to the shell.
    return 0


def cmd_thermal(args):
    """thermal: read temperatures, run the thermal guard, print the verdict. Returns an exit code."""
    # Take one temperature reading of the GPU and CPU.
    s = thermal.sample()
    # Ask the thermal guard if it is safe; True means 'we are about to start heavy work'.
    state, msg = thermal.evaluate(s["gpu_c"], s["cpu_c"], True)
    # Small helper: show a number as '70 C', or say it could not be read.
    fmt = lambda v: "%.0f C" % v if v is not None else "not readable"
    lines = ["GPU temperature: %s." % fmt(s["gpu_c"]), "CPU temperature: %s." % fmt(s["cpu_c"]),
             "Guard result: %s. %s" % (state, msg),
             "Limits: heavy work starts at GPU 84 C or lower, pauses at 86 C, stops at 91 C. gametune never allows 97 C and never overclocks."]
    # Print as text or JSON.
    _out(args, {"sample": s, "state": state, "message": msg}, lines)
    # Pick an exit code: OK -> 0, UNKNOWN -> 2, anything hotter -> 1 (the default of .get).
    return {thermal.OK: 0, thermal.UNKNOWN: 2}.get(state, 1)


def cmd_selftest(args):
    """selftest: run the headless measuring check and print its result. Returns an exit code."""
    # Run the short headless measuring check; the options come straight from the command line.
    r = selftest.run(seconds=args.seconds, scene=args.scene, mangohud_path=args.mangohud_path,
                     allow_unknown_temp=args.allow_unknown_temp)
    # Show the result lines and a final RESULT line.
    _out(args, r, r["lines"] + ["RESULT: %s" % r["status"]])
    # Turn the status text into an exit code: 0 good, 2 skipped, 3 refused or stopped for heat, 1 otherwise.
    return {"OK": 0, "SKIPPED": 2, "REFUSED_HOT": 3, "REFUSED_UNKNOWN_TEMP": 3, "ABORTED_HOT": 3}.get(r["status"], 1)


def cmd_profiles(args):
    """profiles: list the available launch-option profiles. Returns 0."""
    # data is for --json output, lines is for the human-readable output.
    data, lines = [], []
    # Walk the profiles; unproven ones appear only with --experimental.
    for p in profiles.list_profiles(args.experimental):
        # Label each profile honestly as established or unproven.
        label = "ESTABLISHED idea, still measure it" if p.evidence == "established" else "UNPROVEN, test it"
        lines.append("%s [%s%s]: %s Options: %s" % (p.name, label, "" if p.default_on else ", off by default", p.summary, p.params))
        # Keep a compact record of the profile for JSON output.
        data.append({"name": p.name, "evidence": p.evidence, "default_on": p.default_on, "summary": p.summary})
    # Tell the user how to reveal the hidden profiles.
    if not args.experimental:
        lines.append("Unproven profiles are hidden. Show them with: gametune profiles --experimental")
    # List ideas that gametune deliberately refuses to offer, with the reason.
    for name, why in profiles.NOT_OFFERED:
        lines.append("Not offered: %s. %s" % (name, why))
    # Reminder that nothing counts as proven until a report says so.
    lines.append("No profile is proven on your PC until `gametune report` says IMPROVED for your game.")
    _out(args, data, lines)
    return 0


def cmd_apply(args):
    """apply: plan (and with --yes, write) a profile for a game. Returns 0 or 1."""
    # Everything is wrapped in try so a ProfileError becomes a friendly message instead of a crash.
    try:
        # Reject a profile name that does not exist.
        if args.profile not in profiles.PROFILES:
            raise profiles.ProfileError("Unknown profile %s. Run: gametune profiles" % args.profile)
        prof = profiles.PROFILES[args.profile]
        # Unproven profiles need --experimental as a deliberate opt-in.
        if not prof.default_on and not args.experimental:
            raise profiles.ProfileError("%s is unproven and off by default. Add --experimental if you want to test it." % args.profile)
        # Scan hardware because some profiles (shader cache, core pinning) depend on it.
        info = hardware.scan()
        # Keep only the hardware facts the profile builders need.
        hw = {"gpu_vendor": hardware.primary_gpu_vendor(info), "performance_cpus": info["cpu"]["performance_cpus"]}
        # Collect the optional --scale/--output/--fps values the user actually gave.
        params = {k: v for k, v in (("scale", args.scale), ("output", args.output), ("fps", args.fps)) if v is not None}
        # Build the plan (which files would be written) without writing anything yet.
        pl = profiles.plan(args.game, add=(args.profile, params), hw=hw)
        # Check temperatures; starting=False here so this only warns and never blocks.
        s = thermal.sample()
        state, msg = thermal.evaluate(s["gpu_c"], s["cpu_c"], False)
        # Start the output with the thermal message.
        lines = ["Thermal check: %s" % msg]
        # List every file the plan touches; without --yes it is only a dry run.
        for p, (data, _m) in sorted(pl["files"].items()):
            lines.append("%s %s" % ("WILL WRITE" if args.yes else "WOULD WRITE (dry run)", p))
        # The launch file holds the string the user pastes into Steam.
        launch = pl["targets"]["launch"]
        if launch in pl["files"]:
            lines.append("Steam launch options would be: " + pl["files"][launch][0].decode().strip())
        lines += pl["notes"]
        # Only profiles that add wrappers get the anti-cheat warning (not advice-only ones).
        if prof.kind != "advice":
            lines.append(ANTICHEAT)
        # --yes means really write the files (the undo record is saved first).
        if args.yes:
            profiles.apply_plan(args.game, pl, "apply " + args.profile)
            lines.append("Done. Paste the launch options into the game's Steam Launch Options. Undo with: gametune revert %s --game %s" % (args.profile, args.game))
        else:
            # No --yes: remind the user that nothing was written.
            lines.append("Dry run: nothing was written. Add --yes to write these files.")
        _out(args, {"dry_run": not args.yes, "files": sorted(pl["files"]), "profiles": pl["profiles"]}, lines)
        return 0
    # Bad game name or bad option: report and return 1.
    except profiles.ProfileError as exc:
        print("Cannot apply: %s" % exc)
        return 1


def cmd_revert(args):
    """revert: undo one profile or everything gametune wrote. Returns 0 if all succeeded else 1."""
    # Reverting needs either --all or both a profile and a game name.
    if not args.all and not (args.profile and args.game):
        print("Use: gametune revert PROFILE --game NAME   or   gametune revert --all")
        return 1
    try:
        results = []
        # --all with --game reverts just that game; --all alone reverts every game gametune knows.
        if args.all:
            games = [args.game] if args.game else profiles.all_games()
            if not games:
                results.append((True, "Nothing to undo."))
            for g in games:
                results.append(profiles.revert_game(g, args.force))
        # Revert a single profile of a single game.
        else:
            results.append(profiles.revert_profile(args.game, args.profile, force=args.force))
    except profiles.ProfileError as exc:
        print("Cannot revert: %s" % exc)
        return 1
    # Print each (ok, message) result; exit 0 only if every revert worked.
    _out(args, [{"ok": ok, "message": m} for ok, m in results], [m for _ok, m in results])
    return 0 if all(ok for ok, _ in results) else 1


def cmd_report(args):
    """report: compare baseline and tuned MangoHud logs and print the verdict. Returns 0 or 2."""
    # Load the logs (folders become their .csv files) and compute the comparison.
    rep = report.build(report.expand(args.baseline), report.expand(args.tuned), warmup_s=args.warmup, min_runs=args.runs)
    # Save the report as .json and .txt inside gametune's own folder.
    jpath, tpath = report.save(rep)
    if args.json:
        rep["saved"] = [jpath, tpath]
        print(json.dumps(rep, indent=2))
    else:
        # Print the readable report; end='' because render_text already ends with a newline.
        print(report.render_text(rep), end="")
        print("Saved on this computer only: %s and %s" % (jpath, tpath))
    # Exit 2 when there was not enough data to say anything.
    return 0 if rep["verdict"] != "NOT ENOUGH DATA" else 2


def build_parser():
    """Build the argparse parser with all sub-commands and their options."""
    # Create the top-level parser and describe the program.
    ap = argparse.ArgumentParser(prog="gametune", description="Honest game performance helper. Local only. No telemetry. No network.")
    # --version prints the version and exits.
    ap.add_argument("--version", action="version", version="gametune " + __version__)
    # Sub-commands (scan, thermal, ...) are chosen by the first word; dest='cmd' stores which one.
    sub = ap.add_subparsers(dest="cmd")
    def add(name, fn, help_):
        """Create a sub-command parser, give it --json, and remember its handler function."""
        # Each sub-command gets its own parser.
        p = sub.add_parser(name, help=help_)
        # Every sub-command accepts --json.
        p.add_argument("--json", action="store_true", help="machine-readable output")
        # Remember which cmd_ function should run for this sub-command.
        p.set_defaults(fn=fn)
        return p
    # Register the sub-commands and give each its own options below.
    add("scan", cmd_scan, "detect this PC's hardware and helper tools")
    add("thermal", cmd_thermal, "read temperatures and apply the thermal guard")
    p = add("selftest", cmd_selftest, "headless check that measuring works (no real game)")
    # selftest options: length (3 to 20 seconds), which test scene, and where MangoHud is.
    p.add_argument("--seconds", type=int, default=10, help="scene length, 3 to 20")
    p.add_argument("--scene", choices=["auto", "vkcube", "vkmark"], default="auto")
    p.add_argument("--mangohud-path", help="mangohud program, or an extracted tree containing usr/")
    p.add_argument("--allow-unknown-temp", action="store_true", help="run even if no temperature sensor can be read")
    p = add("profiles", cmd_profiles, "list safe, undoable profiles")
    # profiles: optionally include unproven profiles.
    p.add_argument("--experimental", action="store_true", help="also list unproven profiles")
    p = add("apply", cmd_apply, "write launch options for a profile (dry run unless --yes)")
    # apply: the profile name is positional; --game is required; --yes really writes.
    p.add_argument("profile")
    p.add_argument("--game", required=True)
    p.add_argument("--yes", action="store_true", help="really write the files")
    p.add_argument("--experimental", action="store_true")
    p.add_argument("--scale"); p.add_argument("--output"); p.add_argument("--fps")
    p = add("revert", cmd_revert, "undo applied profiles exactly")
    # revert: profile is optional (nargs='?') because --all does not need one.
    p.add_argument("profile", nargs="?")
    p.add_argument("--game"); p.add_argument("--all", action="store_true"); p.add_argument("--force", action="store_true")
    p = add("report", cmd_report, "compare real MangoHud logs before and after")
    # report: --baseline and --tuned each take one or more log files or folders.
    p.add_argument("--baseline", nargs="+", required=True, help="baseline log files or folders")
    p.add_argument("--tuned", nargs="+", required=True, help="tuned log files or folders")
    # Minimum runs per side; the stats code never allows fewer than 3.
    p.add_argument("--runs", type=int, default=3, help="runs needed per side (minimum 3)")
    p.add_argument("--warmup", type=float, default=5.0, help="seconds thrown away at the start of each run")
    return ap


def main(argv=None):
    """Program entry point: parse arguments, run the chosen sub-command, return its exit code."""
    # Parse what the user typed (or the argv list given by tests).
    ap = build_parser()
    args = ap.parse_args(argv)
    # No sub-command given: show the help text and exit 0.
    if not getattr(args, "fn", None):
        ap.print_help()
        return 0
    # Run the chosen sub-command and return its exit code.
    return args.fn(args)


# Also allow `python3 cli.py` style launching.
if __name__ == "__main__":
    sys.exit(main())
