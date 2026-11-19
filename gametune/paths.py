# ==============================================================================
# Program: paths.py
# Programmer: Ethan-da-Tech-Wizard
# Last Modified: November 18, 2026
# Purpose: Filesystem path management for user configurations, steam launch overrides, and logs.
# ==============================================================================

"""Where gametune keeps its own files. Nothing else on the computer is ever written."""
# Resolution order: GAMETUNE_HOME (tests) > $XDG_DATA_HOME > ~/.local/share.
# File role: single source of truth for where gametune stores files. profiles.py, report.py and selftest.py all call sub() instead of building paths,
# which is how the "only write under our own folder" rule is enforced. Tests set GAMETUNE_HOME to a temp folder so they never touch real data.

import os


def home():
    """Data folder. GAMETUNE_HOME overrides it (used by the tests)."""

    # Checked first so tests (and users who want a different location) can redirect everything with one variable.
    override = os.environ.get("GAMETUNE_HOME")
    if override:
        return override

    # XDG_DATA_HOME is the standard Linux location for app data; the fallback is its documented default.
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "gametune")


def sub(*parts):
    """Return a path inside the data folder (does not create it)."""
    return os.path.join(home(), *parts)
