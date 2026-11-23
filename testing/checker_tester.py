"""Evaluate the checker against recorded historical claim outcomes."""

import argparse
from collections import Counter
from pathlib import Path

from checker.claim_checker import replay


def main() -> None:
    """Read claims and evidence, then print the historical confusion matrix."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("history_csv", type=Path, help="Claims history CSV with recorded status.")
    parser.add_argument("--data-dir", type=Path, help="Directory containing the companion input CSVs.")
    args = parser.parse_args()
    data_dir = args.data_dir if args.data_dir is not None else args.history_csv.parent
    results, raw_count = replay(data_dir, history_csv=args.history_csv)
    outcomes = Counter((row["actual_status"], row["predicted_status"]) for row in results)
    print(f"Historical rows: {raw_count:,} | Unique claims: {len(results):,}")
    print("| Recorded outcome | Predicted paid | Predicted denied |")
    print("|---|---:|---:|")
    for status in ("paid", "denied"):
        print(f"| {status.title()} | {outcomes[status, 'paid']:,} | {outcomes[status, 'denied']:,} |")


if __name__ == "__main__":
    main()
