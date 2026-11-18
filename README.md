# gametune

A small, honest helper for people whose PC struggles with games.

**What it does.** It looks at your PC. It lists a few safe changes that can be undone. And it measures
before and after, using real frame-time logs, so you know whether a change helped.

**What it will never do.**
- It never sends anything anywhere. There is no telemetry, no account, and no network code at all.
- It never edits game files or system files. It never uses `sudo`.
- It never overclocks, changes fan speeds, or changes voltages.
- It never claims a speed-up it did not measure. If the numbers are not clear it says
  "NO MEASURABLE CHANGE" or "NOT ENOUGH DATA".

It is free and open source (MIT license). It works on Linux. Windows may come later.

## Install

You need Python 3.8 or newer. Nothing else is required to run it.

From this folder you can run it without installing:

    python3 -m gametune scan

Or install it for your user: `pip install .` (run inside this folder).

For measuring you need three helper programs. On Arch Linux:

    sudo pacman -S --needed mangohud vkmark gamemode

(gamescope is also used for resolution scaling and for the self-test.)

## The commands

Every command also accepts `--json` if a program needs to read the answer.

- `gametune scan` : tells you what hardware it found (processor, graphics card, memory, helper programs).
- `gametune thermal` : reads temperatures and says whether it is safe to do heavy work.
- `gametune selftest` : checks that measuring works on your PC, with no game. It runs a tiny test scene
  for about 10 seconds. If tools are missing it says "SKIPPED, not failed" and what to install.
- `gametune profiles` : lists the changes it can make. Add `--experimental` to also see unproven ones.
- `gametune apply PROFILE --game NAME` : shows what it would write. It writes nothing until you add `--yes`.
- `gametune revert PROFILE --game NAME` or `gametune revert --all` : puts everything back exactly.
- `gametune report --baseline LOGS --tuned LOGS` : compares real MangoHud logs and gives a verdict.

## A full example

1. `gametune apply measure --game MyGame --yes` and paste the printed launch options into the game's
   Steam Launch Options. MangoHud will record frame times while you play.
2. Play the same part of the game three or more times. Use the same settings each time.
3. Turn on one change: `gametune apply fps-cap --game MyGame --fps 60 --yes`. Paste the new options.
4. Play the same part three or more times again.
5. Compare: `gametune report --baseline LOGFOLDER1 --tuned LOGFOLDER2`

Logs go to `~/.local/share/gametune/logs` by default. Keep the baseline runs in their own folder
(move or copy them) before you record the tuned runs.

## How to read the verdict

- **IMPROVED** : a number got better by more than twice the normal run-to-run noise, and by at least 1%.
- **NO MEASURABLE CHANGE** : any difference was too small to tell apart from noise.
- **WORSE** : a number got worse by more than the noise.
- **NOT ENOUGH DATA** : fewer than 3 usable runs on a side. No claim is made.

The first 5 seconds of each run are thrown away (shader compiling makes them unfair). A result is true
for that game, that scene and that PC only.

"1% low" means the speed of the slowest 1% of frames. It shows stutter better than the average does.

## Profiles

Safe ideas (the reason is basic, but still measure on your PC): `render-scale`, `fps-cap`, `measure`,
`shader-cache`, `close-apps` (advice only; it never closes anything).

Unproven, hidden unless you pass `--experimental`: `dxvk-async`, `gamemode`, `core-pinning`.
Raising process priority is not offered because it needs administrator rights.

## Heat safety

Heavy work starts only when the GPU is 84 C or cooler and the CPU is 89 C or cooler.
It pauses at GPU 86 C or CPU 94 C and stops at GPU 91 C or CPU 96 C. You can make these stricter, never looser.

## Anti-cheat warning

Overlays and launch wrappers can upset anti-cheat systems. Do not use them with a game that has
anti-cheat unless its publisher says it is fine. gametune never injects into a game by itself.

## Where files go

Everything is under `~/.local/share/gametune/` (launch options, wrappers, undo records, reports).
Set `GAMETUNE_HOME` to use another folder.

## Honest limits

- The self-test proves the measuring chain works. It does not prove any game gets faster.
- The headless test screen runs at 60 Hz, so its FPS is capped at about 60. That is not a speed score.
- Tested so far on one NVIDIA laptop under Wayland. AMD and Intel paths are written from documentation
  and tested only with fake system folders.

License: MIT. See LICENSE.
