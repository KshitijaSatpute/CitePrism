#!/usr/bin/env python
"""Measure how the CitePrism API holds up under concurrent reviewers.

    python scripts/load_test.py --base http://127.0.0.1:8000 --users 20 --seconds 60

Each simulated reviewer repeatedly opens the library, a completed audit's
review, one reference's evidence and the assistant summary — the read paths a
review team hits all day. Audits themselves are not started: their duration is
bound by external AI and index providers, which a load test should not hammer.

Reports throughput, latency percentiles per endpoint and the error rate, and
exits non-zero when the p95 latency or error rate exceeds the given limits, so
it can gate a release. Run it against a test deployment, never production data
you cannot afford to slow down.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor


def percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile; None for no data."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(fraction * len(ordered))))
    return ordered[rank - 1]


def summarize(samples: dict[str, list[tuple[float, bool]]], seconds: float) -> dict:
    endpoints, total, failures, everything = {}, 0, 0, []
    for name, rows in sorted(samples.items()):
        latencies = [latency for latency, ok in rows if ok]
        errors = sum(1 for _, ok in rows if not ok)
        total += len(rows); failures += errors; everything.extend(latencies)
        endpoints[name] = {
            "requests": len(rows), "errors": errors,
            "p50_ms": round(1000 * percentile(latencies, .50), 1) if latencies else None,
            "p95_ms": round(1000 * percentile(latencies, .95), 1) if latencies else None,
            "max_ms": round(1000 * max(latencies), 1) if latencies else None,
        }
    return {
        "requests": total,
        "throughput_rps": round(total / seconds, 2) if seconds else None,
        "error_rate": round(failures / total, 4) if total else None,
        "p50_ms": round(1000 * statistics.median(everything), 1) if everything else None,
        "p95_ms": round(1000 * percentile(everything, .95), 1) if everything else None,
        "endpoints": endpoints,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="API origin")
    parser.add_argument("--users", type=int, default=10, help="concurrent simulated reviewers")
    parser.add_argument("--seconds", type=float, default=30.0, help="test duration")
    parser.add_argument("--max-p95-ms", type=float, default=1500.0, help="fail above this p95 latency")
    parser.add_argument("--max-error-rate", type=float, default=0.01, help="fail above this error rate")
    parser.add_argument("--output", help="write the summary as JSON")
    arguments = parser.parse_args(argv)

    import requests

    api = arguments.base.rstrip("/") + "/api/v1"
    headers = {"X-CitePrism-Actor": "load-test", "X-CitePrism-Roles": "reviewer"}
    library = requests.get(f"{api}/manuscripts", headers=headers, timeout=30).json().get("items", [])
    audited = [item["latest_audit_id"] for item in library
               if item.get("latest_audit_id") and item.get("latest_audit_state") in ("completed", "completed_with_issues")]
    if not audited:
        print("No completed audit to read. Complete at least one audit on this deployment first.")
        return 2
    references = {}
    for audit_id in audited[:5]:
        review = requests.get(f"{api}/audits/{audit_id}/review", headers=headers, timeout=60).json()
        references[audit_id] = [ref["id"] for ref in review.get("references", [])]

    samples: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    lock = threading.Lock()
    deadline = time.monotonic() + arguments.seconds

    def hit(session, name, url):
        started = time.perf_counter()
        try:
            ok = session.get(url, headers=headers, timeout=60).status_code < 400
        except requests.RequestException:
            ok = False
        with lock:
            samples[name].append((time.perf_counter() - started, ok))

    def reviewer(seed):
        rng = random.Random(seed)
        with requests.Session() as session:
            while time.monotonic() < deadline:
                audit_id = rng.choice(list(references))
                hit(session, "library", f"{api}/manuscripts")
                hit(session, "review", f"{api}/audits/{audit_id}/review")
                if references[audit_id]:
                    hit(session, "reference", f"{api}/audits/{audit_id}/references/{rng.choice(references[audit_id])}")
                hit(session, "assistant_summary", f"{api}/audits/{audit_id}/assistant")
                time.sleep(rng.uniform(0.2, 1.0))  # a person reads between clicks

    began = time.monotonic()
    with ThreadPoolExecutor(max_workers=arguments.users) as pool:
        for index in range(arguments.users):
            pool.submit(reviewer, index)
    summary = summarize(samples, time.monotonic() - began)
    summary.update(users=arguments.users, seconds=arguments.seconds,
                   limits={"p95_ms": arguments.max_p95_ms, "error_rate": arguments.max_error_rate})
    if arguments.output:
        with open(arguments.output, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2)
    print(f"{summary['requests']} requests, {summary['throughput_rps']} req/s, "
          f"p50 {summary['p50_ms']} ms, p95 {summary['p95_ms']} ms, error rate {summary['error_rate']}")
    for name, row in summary["endpoints"].items():
        print(f"  {name:18s} n={row['requests']:<5} p50={row['p50_ms']} ms  p95={row['p95_ms']} ms  errors={row['errors']}")
    passed = (summary["p95_ms"] or 0) <= arguments.max_p95_ms and (summary["error_rate"] or 0) <= arguments.max_error_rate
    print("PASS" if passed else "FAIL: latency or error rate above the limit")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
