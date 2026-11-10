import json
import pandas as pd
from ieee_auditor.models import DocumentData, Finding


def findings_dataframe(findings: list[Finding], profile_name: str | None = None) -> pd.DataFrame:
    df = pd.DataFrame([f.to_dict() for f in findings])
    if profile_name:
        # Keep the active profile in CSV exports so reports are interpretable later.
        df.insert(0, "rule_profile", profile_name)
    return df


def findings_json(document: DocumentData, findings: list[Finding], profile_name: str = "Unspecified profile") -> bytes:
    payload = {
        "filename": document.filename,
        "file_type": document.file_type,
        "page_count": document.page_count,
        "rule_profile": profile_name,
        "summary": {status: sum(1 for f in findings if f.status == status) for status in ("PASS", "FAIL", "REVIEW", "INFO")},
        "findings": [f.to_dict() for f in findings],
        "limitations": [
            "This is an automated pre-submission aid, not an IEEE acceptance or compliance certification.",
            f"The active profile is {profile_name}. A target conference or journal may publish different or newer requirements.",
            "PDF layout checks are heuristic. DOCX style inheritance, floating objects, and final rendering can require visual review.",
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
