from __future__ import annotations
from pathlib import Path
import yaml
from ieee_auditor.models import DocumentData, Finding
from ieee_auditor.parsers.ooxml_parser import parse_docx
from ieee_auditor.parsers.pdf_parser import parse_pdf
from ieee_auditor.checks.audit import run_audit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RULES_DIR = PROJECT_ROOT / "rules"

PROFILE_FILES = {
    "ieee_conference_letter": "ieee_conference_letter.yaml",
    "ieee_transactions_journals_2022": "ieee_transactions_journals_2022.yaml",
}


def list_rule_profiles() -> list[dict[str, str]]:
    """Return the available template profiles for the Streamlit selector."""
    profiles = []
    for profile_id, filename in PROFILE_FILES.items():
        rules_path = RULES_DIR / filename
        with rules_path.open("r", encoding="utf-8") as f:
            rules = yaml.safe_load(f)
        profile = rules.get("profile", {})
        profiles.append({
            "id": profile_id,
            "name": profile.get("name", profile_id),
            "document_type": profile.get("document_type", "unknown"),
            "source_note": profile.get("source_note", ""),
        })
    return profiles


def load_rules(profile_id: str | None = None, path: Path | None = None) -> dict:
    # Keep the conference profile as the default for compatibility with earlier
    # project versions. Users can explicitly select the Transactions profile.
    selected = profile_id or "ieee_conference_letter"
    if path is not None:
        rules_path = path
    else:
        filename = PROFILE_FILES.get(selected)
        if filename is None:
            raise ValueError(f"Unknown IEEE rule profile: {selected}")
        rules_path = RULES_DIR / filename
    with rules_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def analyze_file(data: bytes, filename: str, rules: dict | None = None) -> tuple[DocumentData, list[Finding]]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".docx":
        document = parse_docx(data, filename)
    elif suffix == ".pdf":
        document = parse_pdf(data, filename)
    else:
        raise ValueError("Unsupported file type. Upload a .docx or .pdf file.")
    findings = run_audit(document, rules or load_rules())
    return document, findings
