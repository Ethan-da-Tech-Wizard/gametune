# Program: gametune - __main__.py
# Programmer: Ethan-da-Tech-Wizard (written with Claude, an AI assistant)
# Last Modified: 2026-11-18
# Purpose: Entry point that lets you start the program with `python3 -m gametune`.
"""Lets `python3 -m gametune` run the command line program."""

# sys gives us sys.exit, which hands the exit code back to the shell.
import sys

# Import the real command-line function from cli.py (the dot means 'this package').
from .cli import main

# Run main() and exit with whatever number it returns (0 means success).
sys.exit(main())
