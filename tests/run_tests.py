#!/usr/bin/env python3
"""Run the gametune test suite (used by scripts/check_all.py). Exit 0 only if every test passes."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
suite = unittest.defaultTestLoader.discover(HERE, pattern="test_*.py", top_level_dir=HERE)
result = unittest.TextTestRunner(verbosity=1).run(suite)
sys.exit(0 if result.wasSuccessful() and result.testsRun > 0 else 1)
