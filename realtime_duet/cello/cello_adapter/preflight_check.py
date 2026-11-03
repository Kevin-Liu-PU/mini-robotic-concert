"""Read-only missing-input report. Never approves, connects, or loads a gate."""
import argparse
import ast
import json
import math
from pathlib import Path
import time
from .hardware import digest
from .phase_servo import ServoLimits
from .site_gate import REQUIRED_CHECKS


def inspect(review_path, review_sha256=None):
    from .review_validation import validate_review
    return validate_review(review_path, review_sha256)[2]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--review", required=True)
    p.add_argument("--review-sha256")
    p.add_argument("--report")
    args = p.parse_args()
    report = inspect(args.review, args.review_sha256)
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
    visible = (dict(status=report["status"], issue_count=len(report["issues"]),
                    source_pin_issues=len(report["missing_or_mismatched_pins"]),
                    report=str(Path(args.report).resolve()), hardware_imported=False, robot_connected=False)
               if args.report else {k:v for k,v in report.items() if k != "missing_or_mismatched_pins"})
    print(json.dumps(visible, indent=2))
    return 2 if report["issues"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
