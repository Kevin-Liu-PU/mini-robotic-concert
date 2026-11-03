"""Concrete five-category operator/readback gate. No approval records supplied."""
from pathlib import Path
from .review_validation import (REQUIRED_CHECKS, digest, installation_equal,
                                operator_record_issues)


def validate_session(review, session_id, clock_domain):
    if review.get("session_id") != session_id or review.get("clock_domain") != clock_domain:
        raise ValueError("operator record session/domain mismatch")
    path = Path(review["onsite_checklist_path"]).resolve()
    pins = {str(Path(k).resolve()):v for k,v in review["pins"].items()}
    if pins.get(str(path)) != digest(path):
        raise ValueError("operator record missing from pins or changed")
    issues = operator_record_issues(review)
    if issues:
        raise ValueError("; ".join(x["field"]+": "+x["reason"] for x in issues))
    return True


def validate_connected_session(review, control, receive, geometry):
    validate_session(review, review["session_id"], review["clock_domain"])
    if not control.isConnected() or not receive.isConnected() or not control.isProgramRunning():
        return False
    if int(receive.getSafetyMode()) not in review["allowed_safety_modes"]:
        return False
    return (installation_equal(control.getTCPOffset(), geometry._rec.VERIFIED_TCP)
            and installation_equal([receive.getPayload()], [geometry._rec.VERIFIED_PAYLOAD_KG])
            and installation_equal(receive.getPayloadCog(), geometry._rec.VERIFIED_PAYLOAD_COG))
