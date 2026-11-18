#!/usr/bin/env python3
# Program: gametune - run_tests.py
# Programmer: Ethan-da-Tech-Wizard (written with Claude, an AI assistant)
# Last Modified: 2026-11-18
# Purpose: Runs every test in the tests folder with unittest and exits 0 only if all of them pass.
"""Run the gametune test suite (used by scripts/check_all.py). Exit 0 only if every test passes."""
# os/sys/unittest: paths, exit codes and the test framework.
import os
import sys
import unittest

# The folder this script lives in (the tests folder).
HERE = os.path.dirname(os.path.abspath(__file__))
# Put the project folder first on the import path so `import gametune` works.
sys.path.insert(0, os.path.dirname(HERE))
# Find every file named test_*.py in this folder.
suite = unittest.defaultTestLoader.discover(HERE, pattern="test_*.py", top_level_dir=HERE)
# Run the tests and print results.
result = unittest.TextTestRunner(verbosity=1).run(suite)
# Exit 0 on success; also fail if no tests ran at all.
sys.exit(0 if result.wasSuccessful() and result.testsRun > 0 else 1)
