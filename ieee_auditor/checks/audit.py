from __future__ import annotations
import re
import unicodedata
from collections import Counter
from typing import Iterable

from ieee_auditor.models import DocumentData, Finding, Paragraph

COMMON_ACRONYMS = {"IEEE", "SI", "MKS", "CGS", "SC", "DC", "RMS"}
UPPERCASE_STOPWORDS = {
    "AND", "THE", "ARE", "THAT", "THIS", "WITH", "FROM", "VALUE", "LOWER", "UPPER",
    "GAIN", "AVG", "FIG", "TAB", "TABLE", "IMAGE", "METHOD", "MODEL", "STAGE", "TOTAL",
    "OURS", "BEFORE", "AFTER", "INPUT", "OUTPUT", "RESULTS", "PARAMS", "COMPUTATION",
    "IGHT", "IEEE", "DOI", "NOVEMBER", "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY",
    "JUNE", "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "DECEMBER"
}
TEMPLATE_MARKERS = [
    r"\b1st Given Name Surname\b", r"\b2nd Given Name Surname\b", r"\bdept\. name of\b",
    r"\bXXX-X-XXXX-XXXX-X/", r"\bThis electronic document is a .live. template\b",
    r"\bcomponent, formatting, style, styling, insert\b", r"\bline\s+\d+\s*:",
    r"\bIdentify applicable funding agency here\b", r"\bFailure to remove template text\b",
]


def finding(rule_id: str, category: str, status: str, title: str, message: str,
            location="Document", recommendation="", evidence="", confidence="high") -> Finding:
    return Finding(rule_id, category, status, title, message, location, recommendation, evidence, confidence)


def compact(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _first_content_paragraph(doc: DocumentData) -> Paragraph | None:
    nonempty = [p for p in doc.paragraphs if compact(p.text)]
    if not nonempty:
        return None
    if doc.file_type == "PDF" and doc.pdf_pages:
        # A published PDF can begin with a conference running header. Select the
        # visually prominent title block in the upper portion of page 1 instead.
        page1 = [p for p in nonempty if p.page == 1 and p.bbox_pt]
        if page1:
            page_height = float(doc.pdf_pages[0].get("height_pt", 792))
            title_zone = [p for p in page1 if p.bbox_pt[1] < page_height * 0.34 and len(compact(p.text)) >= 8]
            if title_zone:
                return max(title_zone, key=lambda p: max(p.font_sizes_pt or [0]))
    return nonempty[0]


def _paragraph_match(paragraphs: Iterable[Paragraph], pattern: str):
    rx = re.compile(pattern, re.I)
    return next((p for p in paragraphs if rx.search(compact(p.text))), None)


def _extract_abstract(doc: DocumentData) -> tuple[Paragraph | None, str]:
    ps = doc.paragraphs
    for i, p in enumerate(ps):
        t = compact(p.text)
        if re.match(r"^abstract\s*[-—:]", t, re.I):
            return p, re.sub(r"^abstract\s*[-—:]\s*", "", t, flags=re.I)
        if re.fullmatch(r"abstract", t, re.I):
            nxt = next((q for q in ps[i + 1:] if compact(q.text)), None)
            return p, compact(nxt.text) if nxt else ""
    match = _paragraph_match(ps, r"\babstract\b")
    if match:
        return match, re.sub(r".*?\babstract\b\s*[-—:]?\s*", "", compact(match.text), flags=re.I)
    return None, ""


def _extract_references(doc: DocumentData) -> tuple[int | None, list[tuple[str, str]]]:
    """Extract bibliography labels, including multiple PDF entries in one text block.

    PDF extractors commonly group an entire column of references into one block.
    Each reference still starts on its own visual line, so inspect lines rather
    than the compacted block text. DOCX continues to use paragraph/style metadata.
    """
    start = None
    refs: list[tuple[str, str]] = []
    if doc.file_type == "PDF":
        for i, p in enumerate(doc.paragraphs):
            for line in p.text.splitlines():
                t = compact(line)
                if start is None and re.fullmatch(r"references", t, re.I):
                    start = i
                    continue
                if start is None:
                    continue
                # Some PDFs put the reference key on a separate line from its
                # entry text (notably older FOCS-style proceedings PDFs).
                m = re.match(r"^\[(\d{1,3})\](?:\s+.*)?$", t)
                if m:
                    refs.append((m.group(1), t))
                else:
                    m_key = re.match(r"^\[([A-Za-z]{1,8}\+?\s*\d{2,4})\](?:\s+.*)?$", t)
                    if m_key:
                        refs.append((re.sub(r"\s+", "", m_key.group(1)).upper(), t))
        return start, refs

    for i, p in enumerate(doc.paragraphs):
        t = compact(p.text)
        if start is None and re.fullmatch(r"references", t, re.I):
            start = i
            continue
        if start is None:
            continue
        m = re.match(r"^\[(\d+)\]\s*(.*)$", t)
        if m:
            refs.append((m.group(1), t))
            continue
        m_key = re.match(r"^\[([A-Za-z]{1,8}\+?\s*\d{2,4})\]\s*(.*)$", t)
        if m_key:
            refs.append((re.sub(r"\s+", "", m_key.group(1)).upper(), t))
            continue
        m = re.match(r"^(\d+)[.)]\s+", t)
        if m:
            refs.append((m.group(1), t))
            continue
        if (p.style_id.lower() == "references" or p.style_name.lower() == "references") and p.numbering_num_id not in ("", "0"):
            refs.append((str(len(refs) + 1), t))
    return start, refs


def _find_citations(text: str) -> set[int]:
    """Extract numeric IEEE citations without treating code indexes as citations.

    PDF text can contain source-code examples such as ``base_position[0]`` or
    ``current_position[1]``. A naive square-bracket regex sees the inner index
    as a reference. Citation brackets must therefore start at a token boundary
    (not immediately after an identifier character). Reference numbering starts
    at 1, so a standalone [0] is not a valid IEEE bibliography citation either.
    """
    values: set[int] = set()
    boundary = r"(?<![A-Za-z0-9_])"

    # IEEE citations often express ranges as [3]–[6], not only [3-6].
    range_pattern = boundary + r"\[(\d{1,3})\]\s*[-–—]\s*\[(\d{1,3})\]"
    for m in re.finditer(range_pattern, text):
        start, end = map(int, m.groups())
        if start >= 1 and end >= start and end - start <= 100:
            values.update(range(start, end + 1))

    # Individual citations and compact groups such as [1, 3, 5-7].
    group_pattern = boundary + r"\[([0-9][0-9\s,;–—-]*)\]"
    for m in re.finditer(group_pattern, text):
        content = m.group(1)
        for token in re.split(r"[,;\s]+", content):
            token = token.strip()
            range_match = re.fullmatch(r"(\d+)\s*[-–—]\s*(\d+)", token)
            if range_match:
                start, end = map(int, range_match.groups())
                if start >= 1 and end >= start and end - start <= 100:
                    values.update(range(start, end + 1))
            elif token.isdigit() and int(token) >= 1:
                values.add(int(token))
    return values


def _normalize_citation_key(value: str) -> str:
    return re.sub(r"\s+", "", value).upper()


def _find_citation_keys(text: str) -> set[str]:
    # Distinguish author-year citation keys (e.g. [CEM+ 15]) from equations and ranges.
    return {
        _normalize_citation_key(m.group(1))
        for m in re.finditer(r"\[([A-Za-z]{1,8}\+?\s*\d{2,4})\]", text)
    }


def _pdf_abstract_text(doc: DocumentData, abstract_p: Paragraph, fallback: str) -> str:
    """Approximate the abstract region in a PDF using the first-page geometry.

    This avoids pulling text from the adjacent right-hand column into the abstract.
    """
    if doc.file_type != "PDF" or not abstract_p.bbox_pt or not doc.pdf_pages:
        return fallback
    page = doc.pdf_pages[0]
    mid_x = float(page.get("width_pt", 612)) / 2
    y_start = abstract_p.bbox_pt[1]
    # Abstract formula blocks usually continue a short distance below its first block.
    candidates = [p for p in doc.paragraphs if p.page == 1 and p.bbox_pt
                  and p.bbox_pt[0] < mid_x
                  and y_start - 2 <= p.bbox_pt[1] <= y_start + 82]
    candidates.sort(key=lambda p: (p.bbox_pt[1], p.bbox_pt[0]))
    return " ".join(compact(p.text) for p in candidates) or fallback


def _has_suspicious_symbol(text: str) -> bool:
    for ch in text:
        cat = unicodedata.category(ch)
        if cat in {"Sm", "So"}:
            return True
        if ord(ch) > 127 and cat.startswith("P"):
            # Curly quotes and dashes are normal typography, not automatically prohibited.
            if ch not in "“”‘’–—…" and ch not in "©®™":
                return True
    return bool(re.search(r"(?<!\w)(?:\\[A-Za-z]+|\$[^$]+\$|[A-Za-z]\s*[=<>±×÷]\s*\w)", text))


def run_audit(doc: DocumentData, rules: dict) -> list[Finding]:
    findings: list[Finding] = []
    profile = rules.get("profile", {})
    active_doc_type = str(profile.get("document_type", "conference")).lower()
    profile_name = str(profile.get("name", "selected IEEE template"))
    content_rules = rules.get("content", {})
    title_rules = content_rules.get("title", {})
    abstract_rules = content_rules.get("abstract", {})
    keyword_rules = content_rules.get("keywords", {})
    pagination_rules = content_rules.get("pagination", {})
    if not doc.has_extractable_text:
        findings.append(finding("FILE-001", "File readability", "REVIEW", "No selectable text found",
            "The file has no extractable text. It may be scanned or image-only, so text-based checks cannot run.",
            recommendation="Upload a text-based PDF or DOCX. OCR is not included in this MVP.", confidence="high"))
        return findings
    findings.append(finding("FILE-000", "File readability", "PASS", "Document text extracted",
        f"Extracted text from the {doc.file_type} file.", evidence=f"{len(doc.full_text):,} characters"))

    # Compare detectable publication/layout signals with the selected profile.
    journal_markers = [m for m in doc.pdf_publication_markers if "IEEE journal running header" in m]
    conference_markers = [m for m in doc.pdf_publication_markers if "IEEE conference/proceedings running header" in m]
    mismatch_marker = None
    mismatch_label = None
    if doc.file_type == "PDF" and journal_markers and active_doc_type == "conference":
        mismatch_marker, mismatch_label = journal_markers[0], "journal-style"
    elif doc.file_type == "PDF" and conference_markers and active_doc_type == "journal":
        mismatch_marker, mismatch_label = conference_markers[0], "conference/proceedings-style"
    if mismatch_marker:
        findings.append(finding(
            "PROFILE-001", "Rule profile", "REVIEW",
            f"{mismatch_label.capitalize()} layout detected; selected profile is {profile_name}",
            f"The PDF appears to use a {mismatch_label} layout, but this audit is applying {profile_name}. Some front matter, pagination, and layout rules may differ.",
            recommendation="Select the rule profile that matches the target conference or journal. A publisher-formatted PDF can differ from its authoring template.",
            evidence=mismatch_marker, confidence="high"))

    # Page size
    page_sizes = doc.page_sizes_pt
    if page_sizes:
        expected_w = float(rules["page"]["width_pt"])
        expected_h = float(rules["page"]["height_pt"])
        tol = 2.0
        mismatches = [(i + 1, w, h) for i, (w, h) in enumerate(page_sizes)
                      if not ((abs(w - expected_w) <= tol and abs(h - expected_h) <= tol)
                              or (abs(w - expected_h) <= tol and abs(h - expected_w) <= tol))]
        if mismatches:
            paper_size = rules["page"].get("paper_size", "the selected template's paper size")
            findings.append(finding("LAYOUT-001", "Page layout", "FAIL", f"Page size differs from {paper_size}",
                f"{len(mismatches)} detected section/page size(s) do not match the selected template's {paper_size} page dimensions.",
                location=f"{doc.file_type} page/section geometry", recommendation=f"Use {paper_size} for this template profile, unless the target publication specifies otherwise.",
                evidence="; ".join(f"#{i}: {w:.1f}×{h:.1f} pt" for i,w,h in mismatches[:5])))
        else:
            paper_size = rules["page"].get("paper_size", "expected page size")
            findings.append(finding("LAYOUT-001", "Page layout", "PASS", f"{paper_size} page size detected",
                f"Detected page/section dimensions match {paper_size}.", evidence=f"{expected_w:.0f} × {expected_h:.0f} pt"))

    # Margins: reliable in DOCX section XML, approximate in PDF.
    expected_margins = rules["page"]["margins_pt"]
    if doc.file_type == "DOCX" and doc.margins_pt:
        tol = float(rules["page"].get("margin_tolerance_pt", 3))
        diffs = {k: (doc.margins_pt.get(k), v) for k, v in expected_margins.items()
                 if doc.margins_pt.get(k) is not None and abs(doc.margins_pt[k] - float(v)) > tol}
        if diffs:
            details = ", ".join(f"{k}: {actual:.1f} pt (expected {expected:.1f})" for k,(actual,expected) in diffs.items())
            findings.append(finding("LAYOUT-002", "Page layout", "FAIL", "Section margins differ from template",
                "At least one DOCX section margin is outside the template-derived tolerance.",
                location="DOCX section properties", recommendation="Apply the supplied template's margin settings to all relevant sections.", evidence=details))
        else:
            findings.append(finding("LAYOUT-002", "Page layout", "PASS", "DOCX margins match template profile",
                "The median section margins fall within the template-derived tolerance.", evidence="; ".join(f"{k}={doc.margins_pt[k]:.1f} pt" for k in expected_margins if k in doc.margins_pt)))
    elif doc.file_type == "PDF":
        findings.append(finding("LAYOUT-002", "Page layout", "REVIEW", "PDF margins need visual confirmation",
            "PDF text bounds are not equivalent to page-margin metadata. Text blocks can be inset by layout elements, figures, or columns.",
            recommendation=f"Compare the rendered PDF against {profile_name}; this MVP does not treat text bounding boxes as exact margin metadata.", confidence="low"))
    else:
        findings.append(finding("LAYOUT-002", "Page layout", "REVIEW", "Could not verify margins",
            "The uploaded DOCX did not expose section margin values.", recommendation="Open the document's Layout settings and compare all sections with the template."))

    # Columns
    if doc.file_type == "DOCX":
        required_columns = int(rules["page"].get("body_columns", 2))
        if required_columns in doc.section_columns:
            findings.append(finding("LAYOUT-003", "Page layout", "PASS", f"{required_columns}-column section setting found",
                f"At least one DOCX section uses the expected {required_columns}-column body layout. Mixed front-matter sections may be intentional.",
                evidence=f"Section column settings: {doc.section_columns}", confidence="medium"))
        else:
            findings.append(finding("LAYOUT-003", "Page layout", "REVIEW", f"{required_columns}-column body layout not confirmed",
                f"No {required_columns}-column section setting was detected in the DOCX section properties.",
                recommendation="Check the body layout in Word. This template may use a different layout for title and author front matter."))
    elif doc.pdf_pages:
        content_pages = [p for p in doc.pdf_pages if p["text_block_count"] >= 4]
        two_col_pages = [p for p in content_pages if p["two_columns_detected"]]
        if content_pages and len(two_col_pages) / len(content_pages) >= 0.5:
            findings.append(finding("LAYOUT-003", "Page layout", "PASS", "Two-column layout detected in PDF",
                "Text blocks occupy both halves of most content pages.", evidence=f"{len(two_col_pages)} of {len(content_pages)} content pages" , confidence="medium"))
        else:
            findings.append(finding("LAYOUT-003", "Page layout", "REVIEW", "Two-column layout not reliably detected",
                "A simple PDF block-layout heuristic did not find two columns on most content pages. This can be affected by full-width figures and text extraction order.",
                recommendation="Inspect the PDF visually before treating this as a violation.", confidence="low"))

    # Title and subtitle
    first = _first_content_paragraph(doc)
    if first:
        is_placeholder = bool(re.search(r"paper title|title\*? \(use style", first.text, re.I))
        configured_title_styles = {str(x).lower() for x in title_rules.get("style_ids", [])}
        if not configured_title_styles:
            configured_title_styles = {"papertitle", "paper title"}
        if is_placeholder:
            findings.append(finding("TITLE-001", "Title", "FAIL", "Template title placeholder remains",
                "The first title-like paragraph still contains the template's example title text.",
                location=f"Paragraph {first.index}", recommendation="Replace the placeholder with the actual paper title."))
        elif doc.file_type == "DOCX" and (first.style_id.lower() in configured_title_styles or first.style_name.lower() in configured_title_styles):
            findings.append(finding("TITLE-001", "Title", "PASS", "Paper title style found",
                f"The first non-empty paragraph uses a title style named in {profile_name}.", location=f"Paragraph {first.index}"))
        elif doc.file_type == "PDF" and first.bbox_pt and doc.pdf_pages:
            page_width = float(doc.pdf_pages[0].get("width_pt", 612))
            x0, _, x1, _ = first.bbox_pt
            center_offset = abs(((x0 + x1) / 2) - page_width / 2)
            title_size = max(first.font_sizes_pt or [0])
            if center_offset <= 22 and title_size >= 12:
                findings.append(finding("TITLE-001", "Title", "PASS", "Title appears centered and visually prominent",
                    "The first-page PDF geometry shows a large, centered title block. PDF files do not retain Word style names, so the exact template style cannot be verified.",
                    location=f"Page 1, paragraph {first.index}", evidence=f"Largest detected title text: {title_size:.2f} pt; center offset: {center_offset:.1f} pt", confidence="medium"))
            else:
                findings.append(finding("TITLE-001", "Title", "REVIEW", "PDF title layout needs visual confirmation",
                    "A title-like block was identified, but its size or centering does not clearly match the visual expectations of the template.",
                    location=f"Page 1, paragraph {first.index}", recommendation=f"Compare title size and centering with {profile_name}.", confidence="low"))
        else:
            findings.append(finding("TITLE-001", "Title", "REVIEW", "Title style could not be confirmed",
                "The file does not expose enough editable style metadata to verify the exact paper-title style.",
                location=f"Paragraph {first.index}", recommendation=f"Check the title against {profile_name}.", confidence="low"))
        if active_doc_type == "journal":
            title_issues = []
            if title_rules.get("invited_marker_prohibited") and re.search(r"\(\s*invited\s*\)", first.text, re.I):
                title_issues.append("(Invited) marker")
            if title_rules.get("title_case_not_all_uppercase") and compact(first.text).isupper():
                title_issues.append("all-uppercase title")
            if title_issues:
                findings.append(finding("TITLE-002", "Title", "REVIEW", "Title wording needs review",
                    "The Transactions template asks for upper- and lowercase title text and says not to write '(Invited)' in the title.",
                    location=f"Paragraph {first.index}", recommendation="Use normal upper- and lowercase title capitalization and remove an '(Invited)' marker if present.",
                    evidence=f"Detected: {', '.join(title_issues)}", confidence="medium"))
            else:
                findings.append(finding("TITLE-002", "Title", "REVIEW", "Title content conventions need a human pass",
                    "The title was extracted, but the checker cannot determine whether variables are lightface italic, numbers/units are bold, or formulas with subscripts are too long.",
                    location=f"Paragraph {first.index}", recommendation="Check title capitalization, variable styling, number/unit styling, and formula length against the Transactions template.",
                    evidence=compact(first.text), confidence="low"))
        elif first.has_footnote or first.has_math or _has_suspicious_symbol(first.text) or bool(re.search(r"[*†‡]\s*(?:$|\()", first.text)):
            findings.append(finding("TITLE-002", "Title", "REVIEW", "Possible prohibited title content",
                "The title contains a footnote/math marker or symbol that may violate the selected template instruction.",
                location=f"Paragraph {first.index}", recommendation="Check the title's prohibited content against the selected IEEE template.", evidence=compact(first.text), confidence="medium"))
        else:
            findings.append(finding("TITLE-002", "Title", "PASS", "No obvious prohibited title symbols detected",
                "The title has no detectable footnote/math marker or obvious symbol. Human review is still needed for edge cases.", location=f"Paragraph {first.index}", confidence="medium"))
    else:
        findings.append(finding("TITLE-001", "Title", "FAIL", "Title not found", "No non-empty paragraph was extracted."))

    subtitle = next((p for p in doc.paragraphs if p.style_id.lower() == "papersubtitle" or p.style_name.lower() == "paper subtitle"), None)
    subtitle_allowed = title_rules.get("subtitle_allowed")
    if subtitle_allowed is None and active_doc_type == "journal":
        findings.append(finding("TITLE-003", "Title", "INFO", "Separate subtitle rule not specified by this source",
            "The uploaded Transactions template does not state a separate subtitle prohibition in the extracted instructions.", confidence="low"))
    elif subtitle and compact(subtitle.text) and subtitle_allowed is False:
        findings.append(finding("TITLE-003", "Title", "FAIL", "Subtitle detected",
            "The selected template profile does not allow a separate subtitle.",
            location=f"Paragraph {subtitle.index}", recommendation="Remove the subtitle.", evidence=compact(subtitle.text)))
    elif doc.file_type == "PDF" and subtitle_allowed is False:
        findings.append(finding("TITLE-003", "Title", "INFO", "Subtitle style cannot be verified in PDF",
            "PDFs do not preserve the Word paragraph style needed to prove that no paper-subtitle style was used.", confidence="low"))
    else:
        findings.append(finding("TITLE-003", "Title", "PASS", "No styled subtitle detected",
            "No non-empty paragraph using the template's paper-subtitle style was detected.", confidence="medium"))

    # Abstract and keywords
    abstract_p, abstract_text = _extract_abstract(doc)
    if abstract_p is None or not abstract_text:
        findings.append(finding("CONTENT-001", "Abstract", "FAIL", "Abstract not detected",
            "Could not locate an Abstract heading followed by abstract text.", recommendation="Add the abstract using the template's Abstract style."))
    else:
        findings.append(finding("CONTENT-001", "Abstract", "PASS", "Abstract detected",
            "An Abstract label and following text were found.", location=f"Paragraph {abstract_p.index}"))
        abstract_region_text = _pdf_abstract_text(doc, abstract_p, abstract_text)
        if active_doc_type == "journal":
            # The Transactions source prohibits displayed equations, numbered
            # citations and footnotes in the abstract; it does not prohibit every
            # mathematical symbol or inline notation.
            has_numbered_citation = bool(re.search(r"\[\s*\d+(?:\s*[-,–]\s*\d+)*\s*\]", abstract_region_text))
            looks_like_displayed_equation = any(
                re.fullmatch(r"\s*(?:\([0-9]{1,3}\)|[A-Za-z]\s*=\s*[^,.;]{1,60})\s*", line)
                for line in abstract_region_text.splitlines()
            )
            abstract_has_math_or_footnote = abstract_p.has_footnote or has_numbered_citation or looks_like_displayed_equation
            uncertain_math = abstract_p.has_math
        else:
            abstract_has_math_or_footnote = abstract_p.has_math or abstract_p.has_footnote or _has_suspicious_symbol(abstract_region_text)
            uncertain_math = False
        if abstract_has_math_or_footnote:
            if doc.file_type == "PDF" and doc.pdf_publication_markers:
                message = (f"The abstract appears to contain content prohibited by {profile_name}. This is a formatting-profile finding; a publisher-formatted PDF may differ from its authoring file.")
                recommendation = "Check the target publication's author instructions and source manuscript before modifying a published PDF."
            else:
                message = "The abstract contains a displayed equation, numbered reference citation, or footnote marker prohibited by the Transactions template."
                recommendation = "Keep the abstract as a single stand-alone paragraph and remove displayed equations, numbered citations and footnotes if this profile applies."
            findings.append(finding("CONTENT-002", "Abstract", "REVIEW", "Abstract differs from selected template's content rule",
                message, location=f"Paragraph {abstract_p.index}", recommendation=recommendation, evidence=abstract_region_text[:250], confidence="medium"))
        elif uncertain_math:
            findings.append(finding("CONTENT-002", "Abstract", "REVIEW", "Abstract mathematics needs visual confirmation",
                "An editable math object was found in the abstract, but the parser cannot determine whether it is displayed or inline.",
                location=f"Paragraph {abstract_p.index}", recommendation="The Transactions template prohibits displayed mathematical equations in the abstract; inspect the rendered abstract to determine whether this is displayed math.", confidence="low"))
        else:
            findings.append(finding("CONTENT-002", "Abstract", "PASS", "No obvious prohibited abstract symbols detected",
                "No obvious symbol/math/footnote marker was found in the extracted abstract text.", location=f"Paragraph {abstract_p.index}", confidence="medium"))

    keyword = _paragraph_match(doc.paragraphs, r"^keywords\s*[—–-]") or _paragraph_match(doc.paragraphs, r"^index terms\s*[—–-]")
    if keyword:
        findings.append(finding("CONTENT-003", "Keywords", "PASS", "Keywords line detected",
            f"A Keywords/Index Terms line was found; the selected profile expects '{keyword_rules.get('label', 'Keywords/Index Terms')}'.", location=f"Paragraph {keyword.index}"))
    elif doc.file_type == "PDF":
        findings.append(finding("CONTENT-003", "Keywords", "REVIEW", "Keywords line not detected in PDF",
            f"The selected template profile ({profile_name}) expects a Keywords/Index Terms line, but none was detected in the PDF.",
            recommendation="Check the target journal or conference instructions before treating the missing line as a submission violation.", confidence="low"))
    else:
        findings.append(finding("CONTENT-003", "Keywords", "FAIL", "Keywords line not detected",
            "No Keywords or Index Terms line was found.", recommendation=f"Add the '{keyword_rules.get('label', 'Keywords')}' line immediately after the abstract, following the selected template."))

    if active_doc_type == "journal" and keyword:
        keyword_text = re.sub(r"^\s*index\s+terms\s*[—–-]\s*|^\s*keywords\s*[—–-]\s*", "", compact(keyword.text), flags=re.I)
        terms = [term.strip(" .;:") for term in keyword_text.split(",") if term.strip(" .;:")]
        min_terms = int(keyword_rules.get("min_count", 3))
        max_terms = int(keyword_rules.get("max_count", 4))
        count_ok = min_terms <= len(terms) <= max_terms
        alphabetic_ok = terms == sorted(terms, key=lambda x: x.casefold())
        if count_ok and (alphabetic_ok or not keyword_rules.get("alphabetical_order", False)):
            findings.append(finding("CONTENT-005", "Keywords", "PASS", "Index Terms count and order match template",
                f"Detected {len(terms)} comma-separated terms; count is within {min_terms}–{max_terms} and alphabetical order is satisfied.",
                location=f"Paragraph {keyword.index}", evidence=", ".join(terms), confidence="medium"))
        else:
            reasons = []
            if not count_ok:
                reasons.append(f"detected {len(terms)} terms; expected {min_terms}–{max_terms}")
            if not alphabetic_ok and keyword_rules.get("alphabetical_order", False):
                reasons.append("terms are not in alphabetical order")
            findings.append(finding("CONTENT-005", "Keywords", "FAIL",
                "Index Terms count/order does not match the selected template", "; ".join(reasons) + ". This is a mismatch with the selected Transactions profile, not a universal judgment about every IEEE venue.",
                location=f"Paragraph {keyword.index}", recommendation=f"Provide {min_terms}–{max_terms} distinct terms, separated by commas and sorted alphabetically.",
                evidence=", ".join(terms), confidence="high" if alphabetic_ok is False else "medium"))

    if active_doc_type == "journal" and abstract_p is not None and keyword is not None and doc.file_type == "DOCX":
        abstract_paragraphs = [p for p in doc.paragraphs if abstract_p.index <= p.index < keyword.index and compact(p.text)]
        if len(abstract_paragraphs) == 1:
            findings.append(finding("CONTENT-004", "Abstract", "PASS", "Abstract is a single paragraph",
                "One non-empty paragraph was detected between the Abstract heading and Index Terms.", confidence="medium"))
        else:
            findings.append(finding("CONTENT-004", "Abstract", "FAIL", "Abstract spans multiple paragraphs",
                f"Detected {len(abstract_paragraphs)} non-empty paragraphs between Abstract and Index Terms; the template requires one paragraph.",
                recommendation="Combine the abstract into one continuous paragraph."))

    # Author and affiliation area: the template requires at least one author, but name blocks
    # are not reliably distinguishable from affiliations in plain text.
    if abstract_p is not None:
        before_abstract = [p for p in doc.paragraphs if p.index < abstract_p.index and compact(p.text)]
        author_candidates = [p for p in before_abstract[1:] if not re.search(r"paper title|sub-title|given name surname|dept\. name", p.text, re.I)]
        if author_candidates:
            if doc.file_type == "PDF" and first is not None:
                # In PDFs, author names often occupy the first text block after the title.
                next_block = next((p for p in author_candidates if p.index > first.index and p.page == first.page
                                   and len(compact(p.text)) >= 20), None)
                name_like = 0
                if next_block:
                    author_text = re.sub(r"[0-9∗*†‡]", " ", next_block.text)
                    name_like = len(re.findall(r"\b[A-Z][a-zÀ-ž'’-]+\s+[A-Z][a-zÀ-ž'’-]+\b", author_text))
                if next_block and name_like >= 2:
                    findings.append(finding("AUTHOR-001", "Authors", "PASS", "Author names detected after the title",
                        "A name-like author block appears between the title and abstract. PDF affiliation ordering and institutional details still need visual confirmation.",
                        location=f"Page {next_block.page}, paragraph {next_block.index}", evidence=compact(next_block.text)[:400], confidence="medium"))
                else:
                    findings.append(finding("AUTHOR-001", "Authors", "REVIEW", "Author/affiliation content appears before abstract",
                        "Text blocks exist between the title and abstract, but the parser cannot reliably separate author names, departments, organizations, and email/ORCID fields.",
                        recommendation="Confirm at least one author is listed, names follow the required order, and affiliations are concise.", confidence="low"))
            else:
                findings.append(finding("AUTHOR-001", "Authors", "REVIEW", "Author/affiliation content appears before abstract",
                    "Text blocks exist between the title and abstract, but the parser cannot reliably separate author names, departments, organizations, and email/ORCID fields.",
                    recommendation="Confirm at least one author is listed, names follow left-to-right then next-row order, and affiliations are concise.", confidence="low"))
        else:
            findings.append(finding("AUTHOR-001", "Authors", "FAIL", "Author block not detected before abstract",
                "No likely author/affiliation content was detected between the title and abstract.",
                recommendation="Add at least one author and the required affiliation details."))
    else:
        findings.append(finding("AUTHOR-001", "Authors", "REVIEW", "Author block needs manual review",
            "The abstract boundary could not be found, so the author area was not assessed.", confidence="low"))

    # Headings (structure only; style inheritance is not treated as absolute proof).
    top_headings = [p for p in doc.paragraphs if re.match(r"^[IVXLCDM]+\.\s+\S", compact(p.text))]
    letter_heads = [p for p in doc.paragraphs if re.match(r"^[A-Z]\.\s+\S", compact(p.text))]
    if top_headings:
        wrong_top = [p for p in doc.paragraphs if re.match(r"^\d+\.\s+\S", compact(p.text)) and p.style_id.lower().startswith("heading")]
        if wrong_top:
            findings.append(finding("HEAD-001", "Headings", "REVIEW", "Possible non-Roman top-level heading numbering",
                "Numbered decimal headings using Heading styles were detected; the uploaded template uses uppercase Roman numerals for the top heading level.",
                location=f"Paragraph {wrong_top[0].index}", recommendation="Use uppercase Roman numerals for first-level section headings.", evidence=compact(wrong_top[0].text), confidence="medium"))
        else:
            findings.append(finding("HEAD-001", "Headings", "PASS", "Roman-numeral section headings detected",
                f"Detected {len(top_headings)} top-level headings using the template's Roman-numeral pattern.", confidence="medium"))
    else:
        findings.append(finding("HEAD-001", "Headings", "REVIEW", "Top-level heading pattern not confirmed",
            "No uppercase Roman-numeral section heading was detected in extracted text.", recommendation="Check that main sections use the template's I., II., III. heading pattern.", confidence="medium"))
    if letter_heads:
        findings.append(finding("HEAD-002", "Headings", "PASS", "Lettered subsection headings detected",
            f"Detected {len(letter_heads)} subsection headings matching A., B., C. style.", confidence="medium"))
    else:
        findings.append(finding("HEAD-002", "Headings", "REVIEW", "Lettered subsection headings not detected",
            "No A., B., C. subsection heading pattern was found; this may be correct for a short paper.", confidence="low"))

    if active_doc_type == "journal":
        acknowledgement_heads = [p for p in doc.paragraphs if re.fullmatch(r"acknowledg(?:e)?ments?", compact(p.text), re.I)]
        plural_ack = [p for p in acknowledgement_heads if compact(p.text).lower() in {"acknowledgments", "acknowledgements"}]
        if plural_ack:
            findings.append(finding("ACK-001", "Acknowledgment", "FAIL", "Plural acknowledgment heading detected",
                "The Transactions template specifies the singular heading 'ACKNOWLEDGMENT', even when acknowledging multiple contributors.",
                location=f"Paragraph {plural_ack[0].index}", recommendation="Use the singular heading 'ACKNOWLEDGMENT'."))
        elif acknowledgement_heads:
            findings.append(finding("ACK-001", "Acknowledgment", "PASS", "Singular acknowledgment heading detected",
                "The heading uses the singular form specified by the Transactions template.", location=f"Paragraph {acknowledgement_heads[0].index}"))
        else:
            findings.append(finding("ACK-001", "Acknowledgment", "INFO", "No acknowledgment section detected",
                "The template gives acknowledgment guidance but does not require a separate acknowledgment section in every article.", confidence="low"))

        appendix_head = next((p for p in doc.paragraphs if re.match(r"^appendix(?:\s|$)", compact(p.text), re.I)), None)
        ack_head = acknowledgement_heads[0] if acknowledgement_heads else None
        if appendix_head and ack_head:
            if appendix_head.index < ack_head.index:
                findings.append(finding("ACK-002", "Section order", "PASS", "Appendix precedes acknowledgment",
                    "The detected Appendix section appears before Acknowledgment, as described in the template."))
            else:
                findings.append(finding("ACK-002", "Section order", "FAIL", "Acknowledgment precedes appendix",
                    "The template says appendixes, if present, appear before the acknowledgment section.",
                    recommendation="Move the Appendix section before Acknowledgment."))

        algorithm_mentions = [p for p in doc.paragraphs if re.match(r"^algorithm\s+\d+\b", compact(p.text), re.I)]
        if algorithm_mentions:
            findings.append(finding("ALG-001", "Algorithms", "REVIEW", "Numbered algorithm caption detected",
                f"Detected {len(algorithm_mentions)} numbered algorithm caption(s). The rule source also requires a short title and horizontal rules above/below the title and after the last line.",
                recommendation="Visually verify the algorithm number, short title, and horizontal rules.",
                evidence="; ".join(compact(p.text)[:140] for p in algorithm_mentions[:5]), confidence="medium"))
        else:
            findings.append(finding("ALG-001", "Algorithms", "INFO", "No algorithm caption detected",
                "No algorithm object/caption was detected; this is not a failure when the article contains no algorithms.", confidence="low"))

        if doc.file_type == "DOCX" and any(p.has_footnote for p in doc.paragraphs):
            findings.append(finding("FOOT-001", "Footnotes", "REVIEW", "Footnote references detected",
                "The Transactions template requires superscript numbering and footnotes at the bottom of the column where cited; the parser cannot confirm rendered placement.",
                recommendation="Check that footnotes are numbered separately, displayed at the bottom of the cited column, and not converted to endnotes.", confidence="low"))

    # Figures and tables
    figure_captions = [p for p in doc.paragraphs if re.match(r"^(?:fig(?:ure)?\.?)\s*\d+", compact(p.text), re.I)
                       or p.style_id.lower() == "figurecaption" or p.style_name.lower() == "figure caption"]
    if doc.file_type == "DOCX":
        if doc.images_count:
            if figure_captions:
                findings.append(finding("FIG-001", "Figures", "REVIEW", "Figure captions found",
                    f"Detected {len(figure_captions)} figure caption(s) and {doc.images_count} embedded/anchored image object(s). Caption placement below each figure cannot be fully established from XML because images may float.",
                    recommendation="Visually confirm that each caption is below its figure and figures are cited before they appear.", confidence="medium"))
            else:
                findings.append(finding("FIG-001", "Figures", "FAIL", "Images found without figure captions",
                    f"Detected {doc.images_count} image object(s) but no caption matching 'Fig. 1' was found.",
                    recommendation="Add a figure caption below every figure using 'Fig. n.'"))
        else:
            findings.append(finding("FIG-001", "Figures", "INFO", "No embedded images detected",
                "No drawing/picture objects were found in the DOCX. This check is not applicable unless figures are stored as external objects."))
    else:
        if figure_captions:
            findings.append(finding("FIG-001", "Figures", "REVIEW", "Figure captions detected in PDF",
                f"Detected {len(figure_captions)} caption(s). Image-to-caption ordering and placement need visual confirmation.", confidence="medium"))
        else:
            findings.append(finding("FIG-001", "Figures", "INFO", "No recognizable figure caption detected",
                "No 'Fig. n.' or 'Figure n.' caption was detected. A document without figures does not fail this check; visually confirm only if figures are present.", confidence="low"))

    table_captions = [p for p in doc.paragraphs if re.match(r"^table\s+[ivxlcdm0-9]+\b", compact(p.text), re.I)
                      or p.style_id.lower() == "tablehead" or p.style_name.lower() == "table head"]
    if doc.file_type == "DOCX" and doc.tables_count:
        if table_captions:
            findings.append(finding("TABLE-001", "Tables", "REVIEW", "Table heading detected",
                f"Detected {doc.tables_count} table(s) and {len(table_captions)} table heading(s). The heading-above-table position needs visual confirmation.", confidence="medium"))
        else:
            findings.append(finding("TABLE-001", "Tables", "FAIL", "Table found without a detected heading",
                f"Detected {doc.tables_count} table(s) but no 'TABLE I'/'TABLE 1' heading.", recommendation="Place each table's title above the table."))
    elif doc.file_type == "PDF" and table_captions:
        findings.append(finding("TABLE-001", "Tables", "REVIEW", "Table heading(s) detected; placement needs visual confirmation",
            f"Detected {len(table_captions)} possible table heading(s). PDF text extraction cannot reliably establish table boundaries or heading position.", confidence="low"))
    elif doc.file_type == "PDF":
        findings.append(finding("TABLE-001", "Tables", "INFO", "No table heading marker detected",
            "No TABLE I/TABLE 1 heading was detected. This is not a failure when the paper contains no tables; PDF extraction cannot prove table absence.", confidence="low"))
    else:
        findings.append(finding("TABLE-001", "Tables", "INFO", "No DOCX tables detected", "No Word table objects were detected."))

    if active_doc_type == "journal":
        arabic_table_labels = [p for p in table_captions if re.match(r"^table\s+\d+\b", compact(p.text), re.I)]
        if arabic_table_labels:
            findings.append(finding("TABLE-002", "Tables", "REVIEW" if doc.file_type == "PDF" else "FAIL",
                "Arabic table numbering detected",
                "The Transactions template specifies Roman numerals for table numbering.",
                location=f"Page {arabic_table_labels[0].page}" if arabic_table_labels[0].page else f"Paragraph {arabic_table_labels[0].index}",
                recommendation="Use Roman numerals for table labels, such as TABLE I, TABLE II, and TABLE III.",
                evidence="; ".join(compact(p.text)[:120] for p in arabic_table_labels[:8]), confidence="medium"))
        else:
            findings.append(finding("TABLE-002", "Tables", "PASS" if table_captions else "INFO",
                "Roman table numbering pattern checked" if table_captions else "No table numbering to check",
                "Detected table labels use Roman numerals." if table_captions else "No table captions were detected; this check is not applicable."))

        figure_mentions = re.findall(r"\bFigure\s+\d+\b", doc.full_text, re.I)
        if figure_mentions:
            findings.append(finding("FIG-002", "Figures", "REVIEW", "Long-form Figure references detected",
                "The Transactions template instructs authors to use the abbreviation 'Fig.' when referring to a figure, even at the beginning of a sentence.",
                recommendation="Replace in-text references such as 'Figure 1' with 'Fig. 1', while keeping figure captions in the required caption format.",
                evidence=f"Detected examples: {', '.join(figure_mentions[:10])}", confidence="medium"))
        else:
            findings.append(finding("FIG-002", "Figures", "PASS", "No long-form Figure references detected",
                "No in-text 'Figure n' reference was detected; the template prefers 'Fig. n'.", confidence="medium"))

        if doc.images_count or figure_captions:
            findings.append(finding("FIG-003", "Graphics quality", "REVIEW", "Figure quality and dimensions require visual review",
                "The template specifies minimum resolution (300 dpi for color/grayscale images and 600 dpi for line art/tables), accepted file types, and figure width/depth limits. These cannot be reliably verified from the current text-only extraction.",
                recommendation="Inspect each source graphic's resolution, color space, embedded fonts, and dimensions against the Transactions template.", confidence="low"))

    # References and citations: handle both numeric IEEE labels and conference keys.
    ref_start, refs = _extract_references(doc)
    pre_refs_text = "\n".join(p.text for i, p in enumerate(doc.paragraphs) if ref_start is None or i < ref_start)
    citation_nums = _find_citations(pre_refs_text)
    citation_keys = _find_citation_keys(pre_refs_text)
    if ref_start is None:
        findings.append(finding("REF-001", "References", "FAIL", "References heading not detected",
            "Could not find a References heading.", recommendation="Add a References component heading."))
    else:
        findings.append(finding("REF-001", "References", "PASS", "References heading detected",
            "Found the References section heading.", location=f"Paragraph {ref_start + 1}"))

    if refs:
        labels = [label for label, _ in refs]
        numeric_labels = [int(label) for label in labels if label.isdigit()]
        keyed_labels = {label for label in labels if not label.isdigit()}
        if len(numeric_labels) == len(labels):
            expected = list(range(1, max(numeric_labels) + 1)) if numeric_labels else []
            auto_ref_paras = [p for p in doc.paragraphs if ref_start is not None and p.index > ref_start + 1
                              and (p.style_id.lower() == "references" or p.style_name.lower() == "references")
                              and p.numbering_num_id not in ("", "0")]
            auto_pattern_ok = (doc.file_type != "DOCX" or not auto_ref_paras or
                               all(p.numbering_format == "decimal" and p.numbering_text == "[%1]" for p in auto_ref_paras))
            if doc.file_type == "PDF":
                # PDF text extraction often returns the right column before the left.
                # Validate the label set, not extractor order, and surface this limitation.
                unique_numeric = sorted(set(numeric_labels))
                sequence_ok = unique_numeric == expected and len(unique_numeric) == len(numeric_labels)
            else:
                sequence_ok = numeric_labels == expected
            if sequence_ok and auto_pattern_ok:
                extra = " PDF columns may be extracted out of reading order; visual sequence is not inferred from extraction order." if doc.file_type == "PDF" else ""
                findings.append(finding("REF-002", "References", "PASS", "Numeric reference numbering is consecutive",
                    f"Detected {len(numeric_labels)} numeric reference entries covering [1] through [{max(numeric_labels)}] with no gaps or duplicates.{extra}", confidence="medium" if doc.file_type == "PDF" else "high"))
            elif numeric_labels == expected and not auto_pattern_ok:
                findings.append(finding("REF-002", "References", "REVIEW", "Reference list numbering format needs review",
                    "Reference paragraphs use automatic numbering, but the numbering definition does not clearly match decimal square-bracket labels.",
                    recommendation="Use consecutive decimal labels in square brackets, such as [1], [2], [3].", confidence="medium"))
            else:
                findings.append(finding("REF-002", "References", "FAIL", "Numeric reference numbering is not consecutive",
                    "Detected numeric reference labels contain gaps, duplicates, or out-of-order entries.",
                    recommendation="Number references consecutively in order of first citation.", evidence=f"Detected: {numeric_labels}"))
            missing = sorted(citation_nums - set(numeric_labels))
            if missing:
                findings.append(finding("REF-003", "References", "FAIL", "In-text numeric citations missing from reference list",
                    "Some bracketed numeric citation labels do not have a matching numbered reference entry.",
                    recommendation="Add the missing entries or correct the in-text citation numbers.", evidence=f"Missing reference numbers: {missing}"))
            else:
                findings.append(finding("REF-003", "References", "PASS", "Detected numeric citations have matching entries",
                    f"Found {len(citation_nums)} distinct in-text numeric citation(s); all match a detected reference entry.", confidence="medium"))
        elif keyed_labels and not numeric_labels:
            missing_keys = sorted(citation_keys - keyed_labels)
            key_status = "FAIL" if active_doc_type == "journal" and doc.file_type == "DOCX" else "REVIEW"
            findings.append(finding("REF-002", "References", key_status, "Alphanumeric reference keys detected",
                f"This paper uses alphanumeric keys such as [JL84], while {profile_name} specifies consecutive numeric square-bracket references.",
                recommendation="Use the numeric square-bracket reference style specified by the selected template. If this is a published article or a different venue's paper, choose its applicable profile instead.",
                evidence=f"Detected keys: {', '.join(sorted(keyed_labels)[:20])}", confidence="medium"))
            if missing_keys:
                findings.append(finding("REF-003", "References", "FAIL", "Some citation keys have no detected reference entry",
                    "One or more alphanumeric in-text citations did not match a key in the References section.",
                    recommendation="Check the missing citation keys and reference labels.", evidence=f"Missing keys: {', '.join(missing_keys)}", confidence="medium"))
            else:
                findings.append(finding("REF-003", "References", "PASS", "Alphanumeric citations match reference keys",
                    f"Matched {len(citation_keys)} distinct in-text citation key(s) to {len(keyed_labels)} detected reference key(s).", confidence="medium"))
        else:
            findings.append(finding("REF-002", "References", "REVIEW", "Mixed reference-label styles detected",
                "The bibliography contains a mixture of numeric labels and alphanumeric keys. The selected template expects numeric square-bracket labels.",
                recommendation="Check whether the target venue permits mixed labels; otherwise standardize to its required style.", evidence=f"Labels: {labels[:20]}", confidence="low"))
            findings.append(finding("REF-003", "References", "REVIEW", "Citation matching needs manual review",
                "Mixed numeric and alphanumeric citation styles could not be matched reliably.", confidence="low"))
    else:
        findings.append(finding("REF-002", "References", "REVIEW", "Reference entries could not be numbered reliably",
            "No recognizable numeric or alphanumeric reference labels were extracted.",
            recommendation=f"Review the References list visually and confirm that its style matches {profile_name}.", confidence="low"))
        findings.append(finding("REF-003", "References", "REVIEW", "Citation-to-reference matching not completed",
            "The checker could not obtain a reliable reference-key list.", confidence="low"))

    # Acronym first-use heuristic: scan article body, not author metadata, footer,
    # heading numerals, or bracketed citation identifiers.
    configured_exceptions = {str(x).upper() for x in rules.get("content", {}).get("acronyms", {}).get("exceptions", [])}
    common_acronyms = COMMON_ACRONYMS | configured_exceptions
    acronym_paras = [p for p in doc.paragraphs if (ref_start is None or p.index < ref_start)
                     and compact(p.text)]
    if doc.file_type == "PDF" and abstract_p is not None:
        acronym_paras = [p for p in acronym_paras if p.index > abstract_p.index
                         and not (p.page == 1 and p.bbox_pt and p.bbox_pt[1] > doc.pdf_pages[0]["height_pt"] * 0.88)]
    # Funding acknowledgments contain grant agencies and award codes, not ordinary
    # technical acronyms; do not mix them into the body first-use check.
    ack_index = next((p.index for p in acronym_paras if re.fullmatch(r"acknowledg(?:e)?ments?", compact(p.text), re.I)), None)
    if ack_index is not None:
        acronym_paras = [p for p in acronym_paras if p.index < ack_index]
    # PDF small-caps section headings are often extracted as all-uppercase words.
    acronym_paras = [p for p in acronym_paras if not compact(p.text).isupper()]
    # Exclude all-caps display/table/figure blocks from acronym heuristics. They
    # contain labels such as VALUE, LOWER, AVG and GAIN which are not acronyms.
    prose_blocks = []
    for p in acronym_paras:
        t = compact(p.text)
        letters = [ch for ch in t if ch.isalpha()]
        upper_ratio = (sum(ch.isupper() for ch in letters) / len(letters)) if letters else 1.0
        if len(t) < 25 or upper_ratio > 0.62:
            continue
        if re.match(r"^(?:TABLE|FIG(?:URE)?\.?\s*\d+|INDEX TERMS|KEYWORDS)\b", t, re.I):
            continue
        prose_blocks.append(t)
    body_text = "\n".join(prose_blocks)
    body_text = re.sub(r"\[[^\]]+\]", " ", body_text)  # Remove citation keys and numeric references.
    candidates = []
    for match in re.finditer(r"\b[A-Z]{3,6}\b", body_text):
        acronym = match.group(0)
        if acronym in common_acronyms or acronym in UPPERCASE_STOPWORDS or acronym in {"III", "IV", "VII", "VIII", "IX", "DOI"}:
            continue
        if acronym not in candidates:
            candidates.append(acronym)
    likely_undefined = []
    for acronym in candidates:
        first_match = re.search(r"\b" + re.escape(acronym) + r"\b", body_text)
        if not first_match:
            continue
        first_pos = first_match.start()
        through_first = body_text[:first_pos + len(acronym) + 1]
        # Common expansion pattern: Full Term (ABC), with the acronym at first use.
        definition_pattern = r"\b(?:[A-Za-z][A-Za-z-]*\s+){1,6}\(" + re.escape(acronym) + r"\)"
        defined_near_first_use = bool(re.search(definition_pattern, through_first, re.I))
        if not defined_near_first_use:
            likely_undefined.append(acronym)
    if likely_undefined:
        findings.append(finding("ACRO-001", "Acronyms", "REVIEW", "Possible undefined acronyms",
            "A conservative body-text heuristic found uppercase terms without a nearby expansion. Proper nouns and technical shorthand may still be false positives.",
            recommendation="Manually verify first use for the listed terms; do not treat this heuristic as a definite failure.", evidence=", ".join(likely_undefined[:20]), confidence="low"))
    else:
        findings.append(finding("ACRO-001", "Acronyms", "REVIEW", "Acronym definitions need a human pass",
            "No likely undefined acronym was identified by the heuristic, but this is not proof that every acronym is defined on first use.", confidence="low"))

    # Equation numbering: parenthesized numbers in prose and citations are not equation labels.
    if doc.file_type == "PDF":
        pdf_equation_labels = []
        for p in doc.paragraphs:
            if not p.page:
                continue
            for line in p.text.splitlines()[-2:]:
                match = re.fullmatch(r"\s*\((\d{1,3})\)\s*", line)
                if match:
                    pdf_equation_labels.append((p.page, int(match.group(1))))
        if pdf_equation_labels:
            nums = sorted({num for _, num in pdf_equation_labels})
            labels_text = ", ".join(f"p.{page}:({num})" for page, num in pdf_equation_labels[:12])
            if nums == list(range(1, max(nums) + 1)):
                message = f"Candidate equation labels {nums} have no detected numbering gaps. The PDF text order and alignment still require visual confirmation."
            else:
                message = f"Candidate standalone numbers {nums} may contain equation labels, but gaps/duplicates cannot be confirmed from extracted text alone."
            findings.append(finding("EQ-001", "Equations", "REVIEW", "Possible standalone equation labels detected",
                message, recommendation="Check actual displayed equations for consecutive numbering and right-aligned labels.", evidence=labels_text, confidence="medium"))
        else:
            findings.append(finding("EQ-001", "Equations", "INFO", "No standalone equation labels detected",
                "No isolated parenthesized equation label was found in the PDF text blocks.", confidence="low"))
    else:
        equation_labels = [int(m.group(1)) for m in re.finditer(r"\((\d{1,3})\)", doc.full_text)]
        if equation_labels:
            unique_labels = list(dict.fromkeys(equation_labels))
            findings.append(finding("EQ-001", "Equations", "REVIEW", "Equation-like numbering requires visual confirmation",
                f"Detected parenthesized numbers {unique_labels}. The text parser cannot reliably distinguish equation labels from ordinary parenthetical text.",
                recommendation="Confirm equation numbers are consecutive and flush right.", confidence="low"))
        else:
            findings.append(finding("EQ-001", "Equations", "INFO", "No equation-number pattern detected",
                "No parenthesized numeric equation labels were found."))

    # Pagination differs between the conference and Transactions authoring templates.
    if doc.file_type == "DOCX":
        if pagination_rules.get("page_numbers_allowed_in_template_header"):
            status = "PASS" if doc.has_page_number_field else "REVIEW"
            findings.append(finding("PAGE-001", "Pagination", status,
                "Template page-number field detected" if doc.has_page_number_field else "Template page-number field not detected",
                "The selected Transactions authoring template includes page numbers in the running header." if doc.has_page_number_field else "The selected Transactions authoring template shows page numbers in the running header, but no PAGE field was detected in this DOCX.",
                recommendation="Keep the header/page-number setup from the supplied Transactions template, or confirm the current journal author instructions." if not doc.has_page_number_field else "Retain the running-header pagination from the supplied template.", confidence="medium"))
        elif doc.has_page_number_field:
            findings.append(finding("PAGE-001", "Pagination", "FAIL", "Page-number field detected in DOCX",
                "An editable PAGE field was detected in the Word document; the selected conference template says not to add pagination.",
                recommendation="Remove author-added page-number fields unless the target conference instructs otherwise."))
        else:
            findings.append(finding("PAGE-001", "Pagination", "PASS", "No page-number field detected",
                "No Word PAGE field was detected.", confidence="medium"))
    elif doc.pdf_page_numbers:
        values = [value for _, value in doc.pdf_page_numbers]
        sequential = len(values) >= 2 and all(b == a + 1 for a, b in zip(values, values[1:]))
        if active_doc_type == "journal":
            findings.append(finding("PAGE-001", "Pagination", "INFO", "Journal-style page numbering detected",
                "The Transactions authoring template includes page numbers in its running header, and IEEE production may also change final page numbering.",
                evidence=f"Detected page labels: {values}", confidence="medium"))
        elif doc.pdf_publication_markers and sequential:
            if any("IEEE journal running header" in marker for marker in doc.pdf_publication_markers):
                title = "Sequential journal running-header pagination detected"
                message = "The PDF places sequential page numbers in the top-right IEEE Transactions/Journal running header. This is treated as publication-layout context, not an author pagination failure."
            else:
                title = "Sequential publisher pagination detected"
                message = "The PDF has sequential page numbers plus IEEE copyright/DOI publication markers. These are treated as publisher-layout context, not an author pagination failure."
            findings.append(finding("PAGE-001", "Pagination", "INFO", title,
                message, evidence=f"Page labels: {values}; markers: {', '.join(doc.pdf_publication_markers)}", confidence="high"))
        else:
            findings.append(finding("PAGE-001", "Pagination", "REVIEW", "Page numbers detected in PDF",
                "The PDF contains footer page numbers, but the parser cannot determine whether they were added by the author or publisher.",
                recommendation="Check the target submission instructions; do not infer an author violation from a published proceedings PDF alone.", evidence=f"Page labels: {values}", confidence="low"))
    else:
        findings.append(finding("PAGE-001", "Pagination", "PASS", "No page number detected",
            "No standalone footer page numbers were detected in the PDF.", confidence="medium"))

    # Template remnants: include header/footer text, since the Transactions
    # template places the manuscript-ID placeholder in the running header.
    matched = []
    active_markers = list(TEMPLATE_MARKERS) if active_doc_type == "conference" else []
    active_markers.extend(rules.get("template_markers", []))
    cleanup_text = "\n".join([doc.full_text, *doc.header_footer_text])
    for pattern in active_markers:
        m = re.search(pattern, cleanup_text, re.I)
        if m:
            matched.append(m.group(0))
    if matched:
        findings.append(finding("CLEAN-001", "Template cleanup", "FAIL", "Template instruction text remains",
            "The paper contains one or more phrases copied from the supplied template's instructional/sample text.",
            recommendation="Remove all template guidance, placeholder author information, funding placeholders, and sample content.", evidence="; ".join(matched[:8])))
    else:
        findings.append(finding("CLEAN-001", "Template cleanup", "PASS", "No common template placeholders detected",
            "The most common placeholders and instruction phrases from the uploaded template were not found.", confidence="medium"))

    # Unit formatting checks are only run when the selected source explicitly
    # contains the relevant rule; do not leak conference-specific rules into journals.
    unit_rules = rules.get("units", content_rules.get("units", {}))
    if unit_rules.get("leading_zero"):
        bad_decimal = re.search(r"(?<![\w\d])\.\d+\b", doc.full_text)
        if bad_decimal:
            findings.append(finding("UNIT-001", "Units and notation", "FAIL", "Decimal without leading zero detected",
                "The selected template says to write 0.25 rather than .25.", evidence=bad_decimal.group(0), recommendation="Add a leading zero before decimal points."))
        else:
            findings.append(finding("UNIT-001", "Units and notation", "PASS", "No missing leading zero detected",
                "No decimal written in the .25 form was found.", confidence="medium"))

    # Font/style report for DOCX; a full font policy cannot be validated from text extraction alone.
    if doc.file_type == "DOCX":
        sizes = [s for p in doc.paragraphs for s in p.font_sizes_pt]
        fonts = sorted({f for p in doc.paragraphs for f in p.fonts})
        findings.append(finding("STYLE-001", "Typography", "REVIEW", "Typography profile extracted",
            "DOCX run/style font metadata was extracted, but style inheritance and rendering can override direct formatting.",
            recommendation="Compare the extracted style definitions with the supplied template and inspect a rendered PDF.",
            evidence=f"Font families found: {', '.join(fonts[:12]) or 'not explicitly stored'}; sizes found: {', '.join(str(s) for s in sorted(set(sizes))[:12]) or 'not explicitly stored'} pt", confidence="medium"))
    else:
        fonts = sorted({f for p in doc.paragraphs for f in p.fonts})
        sizes = sorted({s for p in doc.paragraphs for s in p.font_sizes_pt})
        findings.append(finding("STYLE-001", "Typography", "REVIEW", "PDF typography profile extracted",
            "PDF font names and sizes are visible, but template style names are not embedded in PDF text.",
            recommendation=f"Compare body text, headings, captions, and references with {profile_name}.",
            evidence=f"Fonts: {', '.join(fonts[:12]) or 'unknown'}; sizes: {', '.join(str(s) for s in sizes[:15]) or 'unknown'} pt", confidence="medium"))

    if doc.file_type == "PDF" and doc.pdf_publication_markers:
        if journal_markers:
            message = "The PDF includes an IEEE Transactions/Journal running header. It appears to use a journal-style layout, but the header alone does not prove publication status or establish the target venue's current submission rules."
            title = "IEEE journal-style running header detected"
        elif any(m.startswith("©") or m.startswith("DOI ") for m in doc.pdf_publication_markers):
            message = "The PDF includes IEEE copyright/DOI markers. It may be a publisher-formatted proceedings copy; author-submission rules can differ from the final published layout."
            title = "IEEE publication markers detected"
        else:
            message = "The PDF includes publication-context metadata that may affect how front matter and pagination should be interpreted."
            title = "Publication-context markers detected"
        findings.append(finding("PUBLISHER-001", "Publication context", "INFO", title,
            message, evidence="; ".join(doc.pdf_publication_markers), confidence="high"))

    for note in doc.parser_notes:
        findings.append(finding("PARSER-INFO", "Parser notes", "INFO", "Parsing limitation", note, confidence="high"))
    return findings
