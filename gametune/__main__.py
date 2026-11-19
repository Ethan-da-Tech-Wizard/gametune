# ==============================================================================
# Program: __main__.py
# Programmer: Ethan-da-Tech-Wizard
# Last Modified: November 18, 2026
# Purpose: Executable module entry point allowing `python3 -m gametune` execution.
# ==============================================================================

# Propagate main()'s return value as the process exit status.
import sys

from .cli import main

sys.exit(main())
