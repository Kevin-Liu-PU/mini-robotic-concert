"""Shared read-only validation for preflight, reports and runtime admission.

No driver imports, robot connections, approval generation or source execution.
"""
import ast
import hashlib
import importlib.metadata
import ipaddress
import json
import math
import os
from pathlib import Path
import sys
import time
from .phase_servo import ServoLimits

ADAPTER_RUNTIME = ("__init__.py", "__main__.py", "core.py", "transport.py", "hardware.py",
                   "phase_servo.py", "site_gate.py", "review_validation.py", "geometry.py")
UPSTREAM_RUNTIME = ("BaselineControls/play_midi_pieces.py",
                    "SoundClassifier/Data_Collection/recording_a_only.py",
                    "BaselineControls/servo_player.py")
REQUIRED_CHECKS = ("calibration_and_installation", "mode_and_operating_range",
                   "setup_force_and_control_ownership", "attendance_stop_and_authorization",
                   "applicable_site_rules")
PACKAGES = ("numpy", "ur-rtde")
# Numeric readback/serialization allowance only: one nanounit in SI units.
# This is not a permissible physical installation offset or calibration margin.
INSTALLATION_ROUNDOFF_ABS = 1e-9


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def installation_equal(actual, expected):
    try:
        a, b = list(actual), list(expected)
        return len(a) == len(b) and all(math.isfinite(x) and math.isfinite(y) and
            math.isclose(x, y, rel_tol=0.0, abs_tol=INSTALLATION_ROUNDOFF_ABS) for x,y in zip(a,b))
    except (TypeError, ValueError):
        return False


def dependency_versions():
    versions = {"python": ".".join(map(str, sys.version_info[:3]))}
    for package in PACKAGES:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return versions


def required_paths(review):
    root = Path(__file__).parent
    paths = {root/name for name in ADAPTER_RUNTIME}
    if review.get("repo_root"):
        paths |= {Path(review["repo_root"])/name for name in UPSTREAM_RUNTIME}
    for field in ("onsite_checklist_path", "operating_profile_path"):
        if review.get(field):
            paths.add(Path(review[field]))
    return {p.resolve() for p in paths}


def resolve_review_paths(review, base_dir):
    """Resolve resources relative to the manifest, never the caller's cwd.

    Absolute paths may select the operator's existing checkout. This does not
    grant approval or alter the manifest bytes checked by the review hash.
    """
    base = Path(base_dir).resolve()
    def resolved(value):
        path = Path(value).expanduser()
        return str((path if path.is_absolute() else base / path).resolve())
    result = dict(review)
    for field in ("repo_root", "gate_path", "onsite_checklist_path", "operating_profile_path"):
        if result.get(field): result[field] = resolved(result[field])
    result["pins"] = {resolved(key): value for key, value in review.get("pins", {}).items()}
    return result


def _literal(node, environment):
    """Restricted data evaluator, never eval/import/execute calibration code."""
    if isinstance(node, ast.Constant): return node.value
    if isinstance(node, (ast.List, ast.Tuple)): return [_literal(v, environment) for v in node.elts]
    if isinstance(node, ast.Dict): return {_literal(k, environment): _literal(v, environment) for k,v in zip(node.keys,node.values)}
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _literal(node.operand, environment)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _literal(node.left, environment) / _literal(node.right, environment)
    if isinstance(node, ast.Call):
        name = ast.unparse(node.func)
        if name == "np.array": return _literal(node.args[0], environment)
        if name == "float": return float(_literal(node.args[0], environment))
        if name == "os.environ.get":
            key = _literal(node.args[0], environment)
            return environment.get(key, _literal(node.args[1], environment))
    raise ValueError("calibration data expression not supported for static inspection")


def _constants(path, names, environment):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    result = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in names:
                    result[target.id] = _literal(node.value, environment)
    if set(names) - set(result):
        raise ValueError("missing selected calibration constants: " + str(sorted(set(names)-set(result))))
    return result


def calibration_data(review):
    repo, env = Path(review["repo_root"]), review.get("environment", {})
    rec = _constants(repo/UPSTREAM_RUNTIME[1],
                     ("BOW_CONFIGS", "MAX_OUTWARD_DEPTH", "MAX_INWARD_DEPTH",
                      "VERIFIED_TCP", "VERIFIED_PAYLOAD_KG", "VERIFIED_PAYLOAD_COG"), env)
    geom = _constants(repo/UPSTREAM_RUNTIME[0],
                      ("CONFIG_NAME", "U_MIN", "U_MAX", "CALIBRATED_SPEED_MAX", "ACCEL_MAX"), env)
    servo = _constants(repo/UPSTREAM_RUNTIME[2], ("SERVO_DT", "SERVO_LOOKAHEAD", "SERVO_GAIN"), env)
    cfg = rec["BOW_CONFIGS"][geom["CONFIG_NAME"]]
    for label, values, size in [(key, cfg[key], 6) for key in ("frog", "tip", "fsafe", "tsafe")] + [
        ("VERIFIED_TCP", rec["VERIFIED_TCP"], 6), ("VERIFIED_PAYLOAD_COG", rec["VERIFIED_PAYLOAD_COG"], 3)]:
        if len(values) != size or any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            raise ValueError("invalid calibration vector: " + label)
    mass = rec["VERIFIED_PAYLOAD_KG"]
    if type(mass) not in (int, float) or not math.isfinite(mass) or mass < 0:
        raise ValueError("invalid calibrated payload mass")
    length = math.dist(cfg["frog"][:3], cfg["tip"][:3])
    lift = min(math.dist(cfg["frog"][:3], cfg["fsafe"][:3]), math.dist(cfg["tip"][:3], cfg["tsafe"][:3]))
    if not math.isfinite(length) or length <= 0: raise ValueError("invalid calibrated bow length")
    return dict(**rec, **geom, **servo, bow_length_m=length, lift_max_m=lift)


def operator_record_issues(review):
    issues = []
    def add(field, reason): issues.append(dict(field=field, reason=reason))
    value = review.get("onsite_checklist_path")
    if not value or not Path(value).is_file():
        add("onsite_checklist_path", "onsite readiness record is missing")
        return issues
    try:
        data = json.loads(Path(value).read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        add("onsite_checklist_path", str(exc)); return issues
    if data.get("status") != "OPERATOR_REVIEWED_FOR_THIS_SESSION":
        add("onsite_checklist.status", "UNAPPROVED onsite record")
    if data.get("session_id") != review.get("session_id"):
        add("onsite_checklist.session_id", "record belongs to another session")
    if data.get("mode") != review.get("mode"):
        add("onsite_checklist.mode", "record mode differs from requested mode")
    if not isinstance(data.get("operator"), str) or not data["operator"].strip():
        add("onsite_checklist.operator", "onsite operator identity is missing")
    for name in REQUIRED_CHECKS:
        item = data.get("checks", {}).get(name, {})
        if item.get("verified") is not True:
            add("onsite_checklist."+name, "onsite confirmation required")
    rules = data.get("site_rules")
    if not isinstance(rules, str) or not rules.strip():
        add("onsite_checklist.site_rules", "name actual applicable rules or explicitly record none_identified")
    return issues


def validate_review(review_path, expected_sha256=None, session=None, domain=None):
    """Collect all static admission failures using the exact runtime policy."""
    path = Path(review_path).resolve()
    review = resolve_review_paths(json.loads(path.read_text(encoding="utf-8")), path.parent)
    issues = []
    def add(field, reason): issues.append(dict(field=field, reason=reason))
    if not expected_sha256:
        add("review_sha256", "independently reviewed manifest identity not supplied")
    elif digest(path) != expected_sha256.lower(): add("review_sha256", "manifest hash mismatch")
    if review.get("review_status") != "REVIEWED_FOR_ONSITE_TEST": add("review_status", "manifest is UNAPPROVED")
    for field in ("session_id", "clock_domain", "robot_ip", "repo_root"):
        if not isinstance(review.get(field), str) or not review[field].strip(): add(field, "required run identity/path is missing")
    if session is not None and session != review.get("session_id"): add("session_id", "selected session/domain mismatch")
    if domain is not None and domain != review.get("clock_domain"): add("clock_domain", "selected session/domain mismatch")
    try: ipaddress.ip_address(review.get("robot_ip", ""))
    except (ValueError, TypeError):
        if review.get("robot_ip"): add("robot_ip", "invalid IP address")
    expiry = review.get("expires_unix_s")
    if type(expiry) not in (int,float) or not math.isfinite(expiry) or time.time() >= expiry:
        add("expires_unix_s", "session expiry missing/invalid/expired")
    if review.get("open_a_rhythm_approved") is not True: add("open_a_rhythm_approved", "musical A3 substitution not approved")
    if review.get("mode") not in ("air", "contact"): add("mode", "explicit air/contact mode required")
    modes = review.get("allowed_safety_modes")
    if not isinstance(modes,list) or not modes or any(type(v) is not int for v in modes) or not set(modes).issubset({1,2}):
        add("allowed_safety_modes", "cannot broaden upstream NORMAL/REDUCED; select reviewed modes")
    env = {k:v for k,v in os.environ.items() if k.startswith("CELLO_") and k != "CELLO_NO_AUTORECOVER"}
    if env != review.get("environment", {}): add("environment", "runtime CELLO overrides differ from review")
    gate = review.get("gate_path")
    if gate and Path(gate).resolve() != Path(__file__).with_name("site_gate.py").resolve():
        add("gate_path", "external gate needs explicit integration; bundled gate is the supported reviewed path")
    pins = {str(Path(k).resolve()):v for k,v in review.get("pins", {}).items()}
    required = required_paths(review)
    missing = [str(p) for p in sorted(required) if not p.is_file() or pins.get(str(p)) != digest(p)]
    if missing: add("pins", f"{len(missing)} actual execution/calibration/record identities missing or changed")
    # Additional explicitly supplied data pins remain protected; no wholesale
    # upstream directory glob. Unrelated unlisted analysis cannot block a run.
    for file, expected in pins.items():
        if file not in {str(p) for p in required} and (not Path(file).is_file() or digest(file) != expected):
            add("pins."+file, "explicit extra reviewed input changed")
    limits = dict(review.get("limits", {}))
    profile = review.get("operating_profile_path")
    if profile:
        try: limits = {**json.loads(Path(profile).read_text(encoding="utf-8")), **{k:v for k,v in limits.items() if v is not None}}
        except (OSError, ValueError) as exc: add("operating_profile_path", str(exc))
    derived = {}
    try:
        data = calibration_data(review)
        derived = dict(bow_length_m=data["bow_length_m"], period_s=data["SERVO_DT"],
                       lookahead_s=data["SERVO_LOOKAHEAD"], gain=data["SERVO_GAIN"])
        for name,value in derived.items():
            if limits.get(name) is None: limits[name] = value
        if not math.isclose(limits["bow_length_m"], data["bow_length_m"], rel_tol=1e-9):
            add("limits.bow_length_m", "does not match selected calibrated endpoints")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        data = None; add("calibration", str(exc))
    invalid_fields = []
    for field in ServoLimits.__dataclass_fields__:
        if type(limits.get(field)) not in (int,float) or not math.isfinite(limits[field]): invalid_fields.append(field)
    if invalid_fields: add("limits", "existing reviewed operating profile must supply: " + ", ".join(invalid_fields))
    resolved = None
    if not invalid_fields:
        try:
            resolved = ServoLimits(**limits).validate()
            if review.get("mode") == "air" and resolved.lift_m <= 0: add("mode", "air requires reviewed positive clearance")
            if review.get("mode") == "contact" and resolved.lift_m != 0: add("mode", "contact requires explicit zero lift")
            if data:
                checks = ((data["U_MIN"] <= resolved.u_low < resolved.u_high <= data["U_MAX"], "bow interval"),
                          (-data["MAX_OUTWARD_DEPTH"] <= resolved.depth_m <= data["MAX_INWARD_DEPTH"], "depth"),
                          (resolved.lift_m <= data["lift_max_m"], "clearance"),
                          (resolved.speed_max_m_s <= data["CALIBRATED_SPEED_MAX"], "speed"),
                          (resolved.acceleration_max_m_s2 <= data["ACCEL_MAX"], "acceleration"))
                for okay,name in checks:
                    if not okay: add("limits."+name, "exceeds existing calibrated envelope")
        except (TypeError,ValueError) as exc: add("limits", str(exc))
    versions = dependency_versions()
    python_ok = versions["python"].split(".")[:2] in (["3","11"],["3","12"])
    absent = [p for p in PACKAGES if not versions.get(p)]
    expected_versions = review.get("runtime_versions")
    if not python_ok or absent: add("runtime_environment", "use existing upstream Python 3.11/3.12 hardware environment; missing packages: " + ", ".join(absent))
    if expected_versions != versions: add("runtime_versions", "actual dependency versions must be inventoried/reviewed automatically in selected environment")
    issues.extend(operator_record_issues(review))
    report = dict(status="BLOCKED" if issues else "STATIC_VALIDATION_PASSED_LIVE_CHECKS_PENDING",
                  review=str(path), issues=issues, missing_or_mismatched_pins=missing,
                  derived_calibration_values=derived, runtime_versions=versions,
                  connected_checks_required=["installation readback", "fresh telemetry", "force/torque", "actual pose", "control/safety state"],
                  hardware_imported=False, robot_connected=False)
    return review, resolved, report
