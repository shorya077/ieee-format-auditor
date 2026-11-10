# IEEE Format Auditor

A Streamlit MVP that audits DOCX and text-based PDF research papers against one of the supplied IEEE authoring-template profiles. It currently includes:

1. **IEEE Conference Paper — Uploaded Letter Template**: based on the uploaded three-page US Letter conference template.
2. **IEEE Transactions, Journals and Letters — 2022 Authoring Template**: based on the uploaded seven-page 2022 Transactions/Journals/Letters Word template.

Select the target template in the Streamlit sidebar before uploading a paper. The selected profile controls which rules are applied. Do not mix requirements from the two templates.

## Important distinction about the Transactions template

The supplied Transactions document explicitly describes itself as an **authoring template, not a final production template**. It says IEEE may further format accepted articles and that the template's page count is only an estimate. Therefore, a final published journal PDF may look different from this Word template without being defective. The Transactions profile is intended primarily for checking author-prepared manuscripts; PDF findings that cannot be proved from extracted text are sent to `REVIEW` rather than declared failures.

## What it checks

### Shared checks

- US Letter page size and DOCX section margins
- Two-column body-layout signals, while allowing separate front-matter sections
- Title, abstract, keyword/index-term and author-block signals
- Roman-numeral section headings and lettered subsection patterns
- Figure and table caption detection
- Numeric reference numbering and citation-to-reference matching
- Acronym first-use hints (heuristic; manual review required)
- Equation-numbering hints
- Page-number/header/footer context
- Template leftovers and DOCX/PDF typography metadata

### Conference profile

- Uses the uploaded conference template's title/abstract restrictions
- Requires its Keywords/Index Terms line
- Applies the conference template's no-author-added-pagination rule
- Checks conference template layout and reference conventions

### Transactions/Journals/Letters 2022 profile

- US Letter size; margins extracted from the Word template: 0.70 in top/bottom and 0.65 in left/right
- One-column front matter and a two-column body
- Title case (not all-uppercase), and review of title variable/formula styling
- Single-paragraph abstract; no displayed equations, numbered reference citations or footnotes in the abstract
- `Index Terms` with 3–4 comma-separated terms in alphabetical order
- First-use acronym definition rule, even if the acronym was defined in the abstract; exemptions from the template are respected
- Consecutive numeric square-bracket references
- Consecutive parenthesized equation numbers, flush-right placement (visual review)
- Numbered algorithms with short titles and horizontal rules (visual review)
- Figure captions below figures; table headings above tables; Roman-numeral table numbering
- `Fig.` form for in-text figure references; `Table` is not abbreviated
- Graphics review reminders for resolution, dimensions, accepted formats, color space and embedded fonts
- Singular `ACKNOWLEDGMENT` heading and appendix-before-acknowledgment order
- Page-number fields are allowed in the Transactions template running header
- Manuscript-ID placeholder detection in the header/footer
- Optional author biography/photo guidance is not treated as a mandatory content requirement

## Important limitations

1. This is not an official IEEE certification. The two profiles represent only the two uploaded source templates. Always check the current target publication's own author instructions.
2. A Transactions template is a guide for authoring, not a promise that the final proof will have identical page layout or pagination.
3. PDF checks are evidence-based heuristics because PDFs do not preserve Word style names, editable section metadata, or all source-image information.
4. Scanned/image-only PDFs are detected, but OCR is not implemented.
5. Floating figure placement, exact caption position, image resolution, color space, embedded fonts, equation alignment, bibliography punctuation, and inherited typography often require visual review.
6. A `REVIEW` finding is not a failure. It means the parser cannot establish the rule from available evidence or the selected template profile needs human confirmation.
7. Template source files are included in `reference/` for provenance. A template file itself will naturally trigger placeholder/instruction-text findings if audited as though it were a completed paper.

## Run locally

Python 3.10+ is recommended. Python 3.14 may work, subject to package wheel availability in your environment.

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m streamlit run app.py
```

Open the local URL printed by Streamlit, usually `http://localhost:8501`.

## Run tests

```bash
python -m pytest -q
```

Regression tests cover both template profiles, DOCX section geometry and twip conversion, PDF upload, publisher pagination, journal running headers, numeric references grouped in PDF column blocks, and alphanumeric reference keys.

## Project structure

```text
ieee-format-auditor/
├── app.py
├── ieee_auditor/
│   ├── auditor.py
│   ├── models.py
│   ├── reporting.py
│   ├── parsers/
│   │   ├── ooxml_parser.py
│   │   └── pdf_parser.py
│   └── checks/
│       └── audit.py
├── rules/
│   ├── ieee_conference_letter.yaml
│   └── ieee_transactions_journals_2022.yaml
├── reference/
│   ├── IEEE_conference_template_letter.docx
│   └── IEEE_Transactions_Journals_Letters_2022.docx
├── tests/
│   └── test_audit.py
└── requirements.txt
```

## Adding another rule profile

1. Add the new source template under `reference/`.
2. Create a YAML file under `rules/` describing its document type, page geometry, front matter, figures/tables, equations, references, and template-specific instructions.
3. Add the new profile ID and YAML filename to `PROFILE_FILES` in `ieee_auditor/auditor.py`.
4. Add or adjust deterministic checks in `ieee_auditor/checks/audit.py`. Use `PASS`, `FAIL`, `REVIEW`, or `INFO`; use `REVIEW` whenever the parser cannot establish compliance with reliable evidence.

## PDF interpretation notes

PDF extractors may group multiple bibliography entries into one text block and may return columns out of reading order. The parser extracts individual reference labels from lines and validates label sets without assuming extractor order. It supports numeric `[1]` references and alphanumeric keys such as `[JL84]` or `[CEM+15]`; the selected profile determines whether a detected style matches that template.

Running-header page numbers and publisher metadata are retained as context. The conference profile and Transactions profile treat page numbering differently because their uploaded source templates have different pagination behavior.


## Version 5 changes

- Fixed a false-positive citation bug where code examples such as `base_position[0]` were mistaken for IEEE reference citations.
- Numeric citation matching now requires a citation bracket to begin at a token boundary and ignores `[0]` (IEEE reference numbering starts at `[1]`).
- Verified the ICRA conference PDF against the conference profile and the LFX Transactions PDF against the Transactions profile.
- The ICRA PDF's 39 references are extracted; the LFX PDF's 83 references and numeric citations are matched by the v4 parser.

Always select the target profile before auditing: **IEEE Conference Paper** for the ICRA accepted-version paper, and **IEEE Transactions, Journals and Letters (2022)** for the LFX journal-style paper.

- Fixed citation parsing false positives from embedded source-code indexes such as `base_position[0]`.
- CSV and JSON exports now record the selected rule profile, so users can distinguish conference and Transactions audits.
