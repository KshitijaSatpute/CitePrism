"""Machine-readable regression gate; live PostgreSQL skips are explicit."""
import argparse
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--require-postgres', action='store_true')
    parser.add_argument('--report', type=Path, default=ROOT / '.trust-verify' / 'phase2-verification.json')
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    postgres_skips = [str(test) for test, reason in result.skipped if 'postgres' in str(test).lower()]
    accepted = result.wasSuccessful() and not (args.require_postgres and postgres_skips)
    report = {'accepted': accepted, 'tests_run': result.testsRun, 'passed': result.testsRun -
              len(result.errors) - len(result.failures) - len(result.skipped),
              'errors': len(result.errors), 'failures': len(result.failures),
              'skipped': [{'test': str(t), 'reason': reason} for t, reason in result.skipped],
              'live_postgres_required': args.require_postgres,
              'live_postgres_verified': not postgres_skips,
              'scientific_accuracy_measured': False, 'live_llm_calls': False}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(f'Verification report: {args.report}')
    if postgres_skips:
        print('Live PostgreSQL checks were skipped. Run verify_phase2.ps1 -WithPostgres for that gate.')
    return 0 if accepted else 1


if __name__ == '__main__':
    raise SystemExit(main())
