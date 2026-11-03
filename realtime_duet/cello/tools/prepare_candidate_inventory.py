"""Prepare local candidate identities. Never approve or import device code."""
import argparse
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cello_adapter.review_validation import (required_paths, digest, dependency_versions,
                                             calibration_data, resolve_review_paths)


def prepare(repo_root, output_dir, *, session=None, domain=None, operating_profile=None):
    output_dir = Path(output_dir).resolve()
    candidate = json.loads((ROOT / "configs/hardware-review.UNAPPROVED.json").read_text(encoding="utf-8"))
    candidate = resolve_review_paths(candidate, ROOT / "configs")
    candidate["repo_root"] = str(Path(repo_root).expanduser().resolve())
    if operating_profile:
        candidate["operating_profile_path"] = str(Path(operating_profile).expanduser().resolve())
    out = output_dir / "hardware-review.CANDIDATE-UNAPPROVED.json"
    if out.exists() and json.loads(out.read_text(encoding="utf-8")).get("review_status") != "UNAPPROVED":
        raise ValueError("refusing to overwrite a reviewed manifest; choose a new output directory")
    old = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    candidate["session_id"] = session or old.get("session_id") or str(uuid.uuid4())
    candidate["clock_domain"] = domain or old.get("clock_domain") or "local-" + candidate["session_id"]
    candidate["runtime_versions"] = dependency_versions()
    record = json.loads((ROOT / "configs/onsite-checklist.UNAPPROVED.json").read_text(encoding="utf-8"))
    record["session_id"] = candidate["session_id"]
    record["mode"] = candidate["mode"]
    record_path = output_dir / "onsite-checklist.CANDIDATE-UNAPPROVED.json"
    if record_path.exists() and json.loads(record_path.read_text(encoding="utf-8")).get("status") != "UNAPPROVED":
        raise ValueError("refusing to overwrite reviewed onsite facts; choose a new output directory")
    candidate["onsite_checklist_path"] = str(record_path)
    # Inspect source first; incompatible checkout creates no files.
    data = calibration_data(candidate)
    derived = dict(bow_length_m=data["bow_length_m"], period_s=data["SERVO_DT"],
                   lookahead_s=data["SERVO_LOOKAHEAD"], gain=data["SERVO_GAIN"])
    candidate["limits"].update(derived)
    dependencies = required_paths(candidate) - {record_path}
    pins = {str(path): digest(path) for path in sorted(dependencies)}
    output_dir.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    candidate["pins"] = {**pins, str(record_path): digest(record_path)}
    candidate["_notice"] = ("AUTOMATIC LOCAL CANDIDATE ONLY; UNAPPROVED and not executable. "
                             "Paths/identities/versions and four source values are inventoried, not approved. "
                             "Regenerate on the target computer; never reuse another host's candidate.")
    assert candidate["review_status"] == "UNAPPROVED" and candidate["open_a_rhythm_approved"] is False
    assert all(item["verified"] is False for item in record["checks"].values())
    out.write_text(json.dumps(candidate, indent=2), encoding="utf-8")
    report = dict(status="CANDIDATE_UNAPPROVED_NOT_EXECUTABLE", approval_granted=False,
                  candidate=str(out), unique_pin_count=len(candidate["pins"]),
                  derived_from_selected_source=derived, runtime_versions=candidate["runtime_versions"])
    (output_dir / "candidate-inventory.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT / "robotic-cellist")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "configs")
    parser.add_argument("--session")
    parser.add_argument("--clock-domain")
    parser.add_argument("--operating-profile", type=Path)
    args = parser.parse_args()
    try:
        report = prepare(args.repo_root, args.output_dir, session=args.session,
                         domain=args.clock_domain, operating_profile=args.operating_profile)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, "Candidate preparation blocked: " + str(exc) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
