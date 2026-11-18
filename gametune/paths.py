# Program: gametune - paths.py
# Programmer: Ethan-da-Tech-Wizard (written with Claude, an AI assistant)
# Last Modified: 2026-11-18
# Purpose: Works out where gametune keeps its own data folder so every other module builds file paths the same way.

"""Where gametune keeps its own files. Nothing else on the computer is ever written."""
# os is used for environment variables and for joining path pieces safely.
import os


def home():
    # GAMETUNE_HOME wins if set; the tests use this to work in a throwaway folder.
    """Data folder. GAMETUNE_HOME overrides it (used by the tests)."""
    override = os.environ.get("GAMETUNE_HOME")
    if override:
        return override
    # Otherwise follow the XDG rule: use XDG_DATA_HOME, or fall back to ~/.local/share.
    # The `or` picks the fallback when the variable is missing or empty.
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    # Our own folder is always named gametune inside that base folder.
    return os.path.join(base, "gametune")


def sub(*parts):
    """Return a path inside the data folder, built from the given folder/file name pieces."""
    # Join the data folder with any extra pieces, e.g. sub('logs') -> <home>/logs.
    return os.path.join(home(), *parts)
