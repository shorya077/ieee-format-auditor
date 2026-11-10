from pathlib import Path
from ieee_auditor.auditor import analyze_file, load_rules
from ieee_auditor.checks.audit import _find_citations


def test_find_citations_handles_individual_numbers_and_ranges():
    assert _find_citations("Prior work [1], [3-5], and [7, 8].") == {1, 3, 4, 5, 7, 8}


def test_uploaded_reference_template_is_readable_and_auditable():
    project = Path(__file__).resolve().parents[1]
    path = project / "reference" / "IEEE_conference_template_letter.docx"
    document, findings = analyze_file(path.read_bytes(), path.name, load_rules())
    assert document.file_type == "DOCX"
    assert document.has_extractable_text
    assert len(findings) > 10
    assert any(f.rule_id == "LAYOUT-001" for f in findings)
    assert any(f.rule_id == "CLEAN-001" and f.status == "FAIL" for f in findings)


def test_pdf_upload_is_supported():
    import fitz

    pdf = fitz.open()
    page = pdf.new_page(width=612, height=792)
    page.insert_text((48, 60), "A Test Research Paper", fontsize=16)
    page.insert_text((48, 100), "Abstract—This paper tests a simple process.", fontsize=9)
    page.insert_text((48, 125), "Keywords—formatting, IEEE", fontsize=9)
    page.insert_text((48, 170), "I. INTRODUCTION", fontsize=10)
    page.insert_text((48, 195), "Left column body text.", fontsize=9)
    page.insert_text((330, 195), "Right column body text.", fontsize=9)
    payload = pdf.tobytes()
    pdf.close()

    document, findings = analyze_file(payload, "test-paper.pdf")
    assert document.file_type == "PDF"
    assert document.page_count == 1
    assert document.has_extractable_text
    assert any(f.rule_id == "LAYOUT-001" and f.status == "PASS" for f in findings)


def test_pdf_proceedings_footer_is_not_treated_as_word_page_field():
    import fitz
    from ieee_auditor.parsers.pdf_parser import parse_pdf

    pdf = fitz.open()
    for page_no, page_label in enumerate((633, 634), start=1):
        page = pdf.new_page(width=612, height=792)
        page.insert_text((190, 95), "A Published IEEE Paper", fontsize=14)
        page.insert_text((55, 150), "Abstract—A short abstract without formulae.", fontsize=9)
        page.insert_text((55, 760), f"© 2020 IEEE  DOI 10.1109/TEST.2020.12345  {page_label}", fontsize=7)
    payload = pdf.tobytes()
    pdf.close()

    document = parse_pdf(payload, "published.pdf")
    assert document.has_page_number_field is False
    assert document.pdf_page_numbers == [(1, 633), (2, 634)]
    assert "© 2020 IEEE" in document.pdf_publication_markers
    assert any(marker.startswith("DOI 10.1109/") for marker in document.pdf_publication_markers)


def test_alphanumeric_reference_keys_are_extracted_and_matched():
    from ieee_auditor.models import DocumentData, Paragraph
    from ieee_auditor.checks.audit import _extract_references, _find_citation_keys

    paragraphs = [
        Paragraph(index=1, text="Prior work [ABC20] and [XYZ19] is relevant."),
        Paragraph(index=2, text="REFERENCES"),
        Paragraph(index=3, text="[ABC20] A. Author, Example article, 2020."),
        Paragraph(index=4, text="[XYZ19] B. Author, Another article, 2019."),
    ]
    document = DocumentData(filename="x.pdf", file_type="PDF", full_text="\n".join(p.text for p in paragraphs), paragraphs=paragraphs)
    ref_start, refs = _extract_references(document)
    keys = {label for label, _ in refs}
    assert ref_start == 1
    assert keys == {"ABC20", "XYZ19"}
    assert _find_citation_keys(paragraphs[0].text) == keys


def test_pdf_reference_entries_are_extracted_line_by_line_from_column_blocks():
    from ieee_auditor.models import DocumentData, Paragraph
    from ieee_auditor.checks.audit import _extract_references

    paragraphs = [
        Paragraph(index=1, text="Prior work [1] and [2] is relevant.", page=1),
        Paragraph(index=2, text="REFERENCES", page=2),
        # PDF extractors may emit the right column before the left column and
        # place several bibliography entries into one text block.
        Paragraph(index=3, text="[2] B. Author, Second article, 2021.\n[3] C. Author, Third article, 2022.", page=2),
        Paragraph(index=4, text="[1] A. Author, First article, 2020.", page=2),
    ]
    document = DocumentData(
        filename="journal.pdf", file_type="PDF",
        full_text="\n".join(p.text for p in paragraphs), paragraphs=paragraphs,
    )
    start, refs = _extract_references(document)
    assert start == 1
    assert {label for label, _ in refs} == {"1", "2", "3"}


def test_pdf_journal_running_header_page_numbers_are_detected():
    import fitz
    from ieee_auditor.parsers.pdf_parser import parse_pdf

    pdf = fitz.open()
    for page_no in (1, 2):
        page = pdf.new_page(width=612, height=792)
        page.insert_text((49, 27), "IEEE TRANSACTIONS ON TEST SYSTEMS, NOVEMBER 2026", fontsize=7)
        page.insert_text((559, 27), str(page_no), fontsize=7)
        page.insert_text((50, 100), "A body paragraph with enough selectable text.", fontsize=10)
    payload = pdf.tobytes()
    pdf.close()

    document = parse_pdf(payload, "journal.pdf")
    assert document.pdf_page_numbers == [(1, 1), (2, 2)]
    assert any("IEEE journal running header" in marker for marker in document.pdf_publication_markers)


def test_numeric_pdf_reference_matching_ignores_column_extraction_order():
    from ieee_auditor.models import DocumentData, Paragraph
    from ieee_auditor.checks.audit import run_audit

    paragraphs = [
        Paragraph(index=1, text="Example Paper Title", page=1, bbox_pt=(100, 60, 500, 90), font_sizes_pt=[20]),
        Paragraph(index=2, text="Alice Smith, Bob Jones", page=1, bbox_pt=(100, 100, 500, 120)),
        Paragraph(index=3, text="Abstract—A short description.", page=1),
        Paragraph(index=4, text="I. INTRODUCTION\nPrior work [1] and [2] is relevant.", page=1),
        Paragraph(index=5, text="REFERENCES", page=2),
        Paragraph(index=6, text="[2] B. Author, Second article, 2021.\n[3] C. Author, Third article, 2022.", page=2),
        Paragraph(index=7, text="[1] A. Author, First article, 2020.", page=2),
    ]
    document = DocumentData(
        filename="journal.pdf", file_type="PDF",
        full_text="\n".join(p.text for p in paragraphs), paragraphs=paragraphs,
        page_count=2, page_sizes_pt=[(612, 792), (612, 792)],
        pdf_pages=[
            {"page": 1, "width_pt": 612, "height_pt": 792, "two_columns_detected": True, "text_block_count": 6},
            {"page": 2, "width_pt": 612, "height_pt": 792, "two_columns_detected": True, "text_block_count": 3},
        ],
        pdf_publication_markers=["IEEE journal running header: IEEE TRANSACTIONS ON TEST SYSTEMS"],
    )
    findings = run_audit(document, load_rules())
    by_id = {f.rule_id: f for f in findings}
    assert by_id["REF-002"].status == "PASS"
    assert by_id["REF-003"].status == "PASS"
    assert by_id["PROFILE-001"].status == "REVIEW"


def test_transactions_template_profile_loads_and_reads_correct_twip_margins():
    from ieee_auditor.auditor import list_rule_profiles

    project = Path(__file__).resolve().parents[1]
    path = project / "reference" / "IEEE_Transactions_Journals_Letters_2022.docx"
    rules = load_rules("ieee_transactions_journals_2022")
    document, findings = analyze_file(path.read_bytes(), path.name, rules)

    assert rules["profile"]["document_type"] == "journal"
    assert rules["profile"]["authoring_template_not_final_production"] is True
    assert ("top", document.margins_pt["top"]) == ("top", 50.4)
    assert document.margins_pt["right"] == 46.8
    assert document.section_columns == [1, 2]
    assert document.has_page_number_field is True
    assert any(p["id"] == "ieee_transactions_journals_2022" for p in list_rule_profiles())

    by_id = {f.rule_id: f for f in findings}
    assert by_id["LAYOUT-002"].status == "PASS"
    assert by_id["PAGE-001"].status == "PASS"


def test_transactions_profile_does_not_apply_conference_no_page_number_rule():
    from ieee_auditor.models import DocumentData
    from ieee_auditor.checks.audit import run_audit

    document = DocumentData(
        filename="journal.docx", file_type="DOCX", full_text="A title\nAbstract—A paragraph.\nIndex Terms—alpha, beta, gamma",
        paragraphs=[], has_page_number_field=True, has_extractable_text=True,
    )
    findings = run_audit(document, load_rules("ieee_transactions_journals_2022"))
    pagination = next(f for f in findings if f.rule_id == "PAGE-001")
    assert pagination.status == "PASS"


def test_conference_profile_remains_default():
    assert load_rules()["profile"]["document_type"] == "conference"


def test_find_citations_does_not_treat_code_array_indexes_as_references():
    text = "Use base_position[0], current_position[1], and values[2] before citing [1], [3]–[5]."
    assert _find_citations(text) == {1, 3, 4, 5}


def test_find_citations_ignores_zero_reference_label():
    # IEEE reference numbering begins at [1]; [0] is typically an array index
    # or an extraction artifact, not a valid bibliography reference.
    assert _find_citations("array index [0], cited work [1].") == {1}


def test_csv_and_json_reports_record_the_selected_rule_profile():
    import json
    from ieee_auditor.models import DocumentData, Finding
    from ieee_auditor.reporting import findings_dataframe, findings_json

    doc = DocumentData(filename="sample.pdf", file_type="PDF", full_text="sample", page_count=1)
    findings = [Finding("TEST-001", "Test", "PASS", "Sample", "All good")]
    profile = "IEEE Transactions, Journals and Letters (2022)"
    df = findings_dataframe(findings, profile)
    payload = json.loads(findings_json(doc, findings, profile))
    assert df.loc[0, "rule_profile"] == profile
    assert payload["rule_profile"] == profile


def test_transactions_index_terms_unsorted_is_reported_as_profile_failure():
    from ieee_auditor.models import DocumentData, Paragraph
    from ieee_auditor.checks.audit import run_audit

    paragraph = Paragraph(index=1, text="Index Terms—Light field perception, cross-form modeling, salient object detection, semantic segmentation")
    document = DocumentData(
        filename="transactions.pdf", file_type="PDF", full_text=paragraph.text,
        paragraphs=[paragraph], page_count=1, page_sizes_pt=[(612, 792)],
    )
    findings = run_audit(document, load_rules("ieee_transactions_journals_2022"))
    term_check = next(f for f in findings if f.rule_id == "CONTENT-005")
    assert term_check.status == "FAIL"
    assert "not in alphabetical order" in term_check.message
