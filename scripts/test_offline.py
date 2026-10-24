"""Run the entire offline suite, fail network access, identify live-only exclusion."""
import os
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def network_forbidden(*args, **kwargs):
    raise AssertionError('Network access is forbidden in the offline test suite')


def main():
    print('EXCLUDED: tests/test_v2.py (historical pytest/live-account integration)')
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    with patch.object(socket.socket, 'connect', network_forbidden), \
         patch.object(socket.socket, 'connect_ex', network_forbidden), \
         patch.object(socket, 'create_connection', network_forbidden):
        for path in sorted((ROOT / 'tests').glob('test_*.py')):
            if path.name != 'test_v2.py':
                suite.addTests(loader.discover(str(ROOT / 'tests'), pattern=path.name))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
