"""Distribution tests must not depend on the original operator's home."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

class RepositoryPortabilityTests(unittest.TestCase):
    def test_test_runner_and_fixtures_do_not_depend_on_operator_home(self):
        files = [ROOT/'tests/run_tests.py', ROOT/'tests/serve_fixture.py'] + list((ROOT/'tests/regression').glob('test_*.py'))
        bad = []
        for path in files:
            if path == Path(__file__).resolve():
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if '/root/.hermes/' in line:
                    bad.append(f'{path.relative_to(ROOT)}:{number}')
        self.assertEqual(bad, [], 'Use tempfile/PATH or explicit test env overrides, not operator private directories')

if __name__ == '__main__':
    unittest.main()
