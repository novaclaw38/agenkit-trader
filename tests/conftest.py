import sys
from pathlib import Path

# Make tests/fakes.py importable from nested test packages.
_TESTS = str(Path(__file__).parent)
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)
