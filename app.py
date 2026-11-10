from pathlib import Path
import streamlit as st

from ieee_auditor.auditor import analyze_file, list_rule_profiles, load_rules
from ieee_auditor.reporting import findings_dataframe, findings_json

st.set_page_config(page_title="IEEE Format Auditor", page_icon="📝", layout="wide")

st.title("IEEE Format Auditor")

with st.sidebar:
    st.header("Rule profile")
    profiles = list_rule_profiles()
    profile_by_id = {p["id"]: p for p in profiles}
    profile_id = st.selectbox(
        "Choose the target template",
        options=[p["id"] for p in profiles],
        format_func=lambda value: profile_by_id[value]["name"],
        index=0,
    )
    rules = load_rules(profile_id)
    selected_profile = profile_by_id[profile_id]
    st.caption(f"Rule source: {rules['profile']['source']}")
    if rules["profile"].get("authoring_template_not_final_production"):
        st.info("This is an authoring template. IEEE may reformat the accepted article during production; published PDFs may not match this Word layout exactly.")
    else:
        st.info("This profile is extracted from a specific conference template. Other IEEE conferences may use different requirements.")
    st.caption(selected_profile.get("source_note", ""))
    st.caption("Select the conference profile for conference submissions and the Transactions profile for journal submissions. The selected profile is recorded in exported reports.")
    st.markdown("**Supported uploads**")
    st.write("DOCX and text-based PDF")
    st.markdown("**Not included yet**")
    st.write("Scanned-PDF OCR, automatic document repair, exact visual overlays, and per-journal custom rules beyond the supplied template.")

uploaded = st.file_uploader("Upload a research paper", type=["docx", "pdf"], help="Maximum size is controlled by your Streamlit configuration.")

if uploaded:
    try:
        file_bytes = uploaded.getvalue()
        with st.spinner(f"Parsing document and applying {rules['profile']['name']} rules..."):
            document, findings = analyze_file(file_bytes, uploaded.name, rules)
        counts = {s: sum(1 for f in findings if f.status == s) for s in ("PASS", "FAIL", "REVIEW", "INFO")}

        if document.pdf_publication_markers:
            st.info(
                "Publication-context markers were detected: "
                + "; ".join(document.pdf_publication_markers)
                + ". These markers help interpret publisher/journal layout features; they do not prove that every author-formatting rule is satisfied."
            )

        st.caption("FAIL = a detectable mismatch with the selected template. REVIEW = evidence is incomplete or the PDF may follow a different venue profile. INFO = context or a check that is not applicable.")
        st.subheader("Audit summary")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Passed", counts["PASS"])
        c2.metric("Failed", counts["FAIL"])
        c3.metric("Needs review", counts["REVIEW"])
        c4.metric("Informational", counts["INFO"])

        if counts["FAIL"]:
            st.error(f"{counts['FAIL']} failure(s) detected. Fix these before submission.")
        elif counts["REVIEW"]:
            st.warning("No deterministic failures were detected, but some checks still need manual review.")
        else:
            st.success("No issues were detected by the active rule set. This is not an official compliance guarantee.")

        st.subheader("Findings")
        df = findings_dataframe(findings, selected_profile["name"])
        status_filter = st.multiselect("Filter by status", ["FAIL", "REVIEW", "PASS", "INFO"], default=["FAIL", "REVIEW", "PASS", "INFO"])
        category_options = sorted(df["category"].dropna().unique().tolist())
        category_filter = st.multiselect("Filter by category", category_options, default=category_options)
        filtered = df[df["status"].isin(status_filter) & df["category"].isin(category_filter)]
        display_cols = ["status", "category", "title", "location", "message", "recommendation", "evidence"]
        st.dataframe(filtered[display_cols], use_container_width=True, hide_index=True,
                     column_config={
                         "status": st.column_config.TextColumn("Status", width="small"),
                         "category": st.column_config.TextColumn("Category", width="medium"),
                         "title": st.column_config.TextColumn("Check", width="medium"),
                         "location": st.column_config.TextColumn("Location", width="small"),
                         "message": st.column_config.TextColumn("Finding", width="large"),
                         "recommendation": st.column_config.TextColumn("Recommended action", width="large"),
                         "evidence": st.column_config.TextColumn("Evidence", width="large"),
                     })

        st.subheader("Export report")
        e1, e2 = st.columns(2)
        with e1:
            csv_data = filtered.to_csv(index=False).encode("utf-8")
            st.download_button("Download findings CSV", csv_data, file_name=f"{Path(uploaded.name).stem}_ieee_audit.csv", mime="text/csv", use_container_width=True)
        with e2:
            st.download_button("Download full JSON report", findings_json(document, findings, selected_profile["name"]),
                               file_name=f"{Path(uploaded.name).stem}_ieee_audit.json", mime="application/json", use_container_width=True)

        with st.expander("Document extraction details"):
            st.write({
                "File": document.filename,
                "Type": document.file_type,
                "Extracted characters": len(document.full_text),
                "DOCX section columns": document.section_columns,
                "DOCX margins (pt)": document.margins_pt,
                "Embedded DOCX images": document.images_count,
                "DOCX tables": document.tables_count,
                "DOCX header/footer text": document.header_footer_text,
                "PDF page count": document.page_count,
                "PDF page labels": document.pdf_page_numbers,
                "Publisher markers": document.pdf_publication_markers,
                "Parser notes": document.parser_notes,
            })
            if document.pdf_pages:
                st.dataframe(document.pdf_pages, use_container_width=True, hide_index=True)

    except Exception as exc:
        st.error(f"Could not audit this file: {exc}")
        st.exception(exc)
else:
    st.info("Upload a DOCX or text-based PDF to run the audit.")
    st.markdown("**What this version checks:** page size, accessible margin metadata, two-column layout signals, title and abstract patterns, keywords, heading patterns, figure/table captions, numeric or author-year reference keys, acronym first-use hints, equation labels, pagination, template leftovers, and decimal leading zeros.")

st.divider()
st.caption("Built as an automated formatting aid. Always use the exact template and author instructions issued by the target IEEE conference.")
