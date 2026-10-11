"""Run synthetic tests with camera, audio, GUI, network and media-write guards."""
import argparse
import json
from pathlib import Path
import sys
import unittest

from tests.support.isolation import HardwareIsolation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pattern', default='test*.py')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    with HardwareIsolation() as guard:
        self_checks = guard.self_check()
        suite = unittest.defaultTestLoader.discover(str(root/'tests'), pattern=args.pattern)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    summary = dict(tests=result.testsRun, failures=len(result.failures),
                   errors=len(result.errors), skipped=len(result.skipped),
                   isolation_self_checks=self_checks, forbidden_attempts=guard.attempts,
                   passed=result.wasSuccessful() and not guard.attempts,
                   evidence='Synthetic/mock only; no hardware or recorded-input validation')
    if args.report:
        args.report.write_text(json.dumps(summary, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(summary))
    return 0 if summary['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
