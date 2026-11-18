"""Where gametune keeps its own files. Nothing else on the computer is ever written."""
import os


def home():
    """Data folder. GAMETUNE_HOME overrides it (used by the tests)."""
    override = os.environ.get("GAMETUNE_HOME")
    if override:
        return override
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "gametune")


def sub(*parts):
    return os.path.join(home(), *parts)
