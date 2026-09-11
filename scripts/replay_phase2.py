"""Generate the supplied-paper regression example without API calls or credentials."""
import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.evidence_fixtures import scored
from src.evidence.review import report_html


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / '.trust-verify' / 'phase2-replay')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        data = scored(Path(directory))
    (args.output / 'offline_replay.json').write_text(json.dumps(data, indent=2), encoding='utf-8')
    report = report_html(data).replace('<h1>', '<p><strong>OFFLINE REGRESSION REPLAY — simulated assessments, not a fresh provider audit.</strong></p><h1>', 1)
    (args.output / 'offline_replay.html').write_text(report, encoding='utf-8')
    print('Offline replay complete. No external APIs were called. No scientific accuracy score is claimed.')
    print(json.dumps(data['review']['counts'], indent=2))
    print(f'Artifacts: {args.output.resolve()}')


if __name__ == '__main__':
    main()
