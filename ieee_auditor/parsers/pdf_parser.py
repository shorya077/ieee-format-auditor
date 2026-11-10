from __future__ import annotations
import re
import fitz
from ieee_auditor.models import DocumentData, Paragraph


def parse_pdf(data: bytes, filename: str) -> DocumentData:
    try:
        pdf = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ValueError(f"Could not open this PDF: {exc}") from exc

    paragraphs: list[Paragraph] = []
    page_sizes = []
    page_summaries = []
    all_text = []
    page_numbers: list[tuple[int, int]] = []
    publication_markers: list[str] = []

    for page_no, page in enumerate(pdf, start=1):
        rect = page.rect
        page_sizes.append((float(rect.width), float(rect.height)))
        raw = page.get_text("dict", sort=True)
        blocks = []
        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            block_text_parts = []
            fonts, sizes = [], []
            bold = italic = False
            for line in block.get("lines", []):
                line_parts = []
                for span in line.get("spans", []):
                    st = span.get("text", "")
                    if st:
                        line_parts.append(st)
                        if span.get("font") and span["font"] not in fonts:
                            fonts.append(span["font"])
                        if span.get("size"):
                            sizes.append(round(float(span["size"]), 2))
                        flags = span.get("flags", 0)
                        bold = bold or bool(flags & 16)
                        italic = italic or bool(flags & 2)
                if line_parts:
                    block_text_parts.append("".join(line_parts))
            text = "\n".join(block_text_parts).strip()
            if not text:
                continue
            bbox = tuple(float(v) for v in block.get("bbox", (0, 0, 0, 0)))
            blocks.append({"text": text, "bbox": bbox, "fonts": fonts, "sizes": sizes, "bold": bold, "italic": italic})
            paragraphs.append(Paragraph(
                index=len(paragraphs) + 1,
                text=text,
                page=page_no,
                fonts=fonts,
                font_sizes_pt=sorted(set(sizes)),
                is_bold=bold,
                is_italic=italic,
                bbox_pt=bbox,
            ))
            all_text.append(text)

            # Publisher/journal metadata are context signals, not proof that a PDF
            # follows the currently selected author template.
            compact_text = re.sub(r"\s+", " ", text)
            copyright_match = re.search(r"©\s*20\d{2}\s+IEEE", compact_text, re.I)
            if copyright_match:
                marker = copyright_match.group(0)
                if marker not in publication_markers:
                    publication_markers.append(marker)
            doi = re.search(r"\bDOI\s+10\.\d{4,9}/[A-Z0-9._;()/:+-]+", compact_text, re.I)
            if doi:
                marker = doi.group(0).rstrip(".,")
                if marker not in publication_markers:
                    publication_markers.append(marker)

            # Detect IEEE Transactions/Journal running headers. These are common
            # in journal PDFs and differ from the uploaded conference template.
            if bbox[1] <= rect.height * 0.06:
                header_lines = []
                for line in block.get("lines", []):
                    line_text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
                    if line_text and not re.fullmatch(r"\d{1,4}", line_text):
                        header_lines.append(line_text)
                header_text = " ".join(header_lines)
                journal_header = re.search(
                    r"\bIEEE\s+(?:TRANSACTIONS|JOURNAL)\s+ON\s+[A-Z0-9 ,&-]{8,140}",
                    re.sub(r"\s+", " ", header_text), re.I)
                if journal_header:
                    marker = "IEEE journal running header: " + journal_header.group(0).strip(" ,")
                    if marker not in publication_markers:
                        publication_markers.append(marker)
                conference_header = re.search(
                    r"\b(?:\d{1,3}(?:st|nd|rd|th)\s+)?(?:Annual\s+)?IEEE\s+(?:Symposium|Conference|Workshop|Proceedings)\b[^\n]{0,150}",
                    re.sub(r"\s+", " ", header_text), re.I)
                if conference_header:
                    marker = "IEEE conference/proceedings running header: " + conference_header.group(0).strip(" ,")
                    if marker not in publication_markers:
                        publication_markers.append(marker)

            # A vertical arXiv stamp on page 1 indicates a preprint copy. Do not
            # mistake arXiv citations in the bibliography for document metadata.
            if page_no == 1 and bbox[0] <= rect.width * 0.08 and (bbox[3] - bbox[1]) >= rect.height * 0.20:
                arxiv_match = re.search(r"\barXiv:\s*\d{4}\.\d{4,5}(?:v\d+)?", compact_text, re.I)
                if arxiv_match:
                    marker = "arXiv identifier: " + arxiv_match.group(0)
                    if marker not in publication_markers:
                        publication_markers.append(marker)

            # Page labels may be in a top-right running header (journal PDFs) or
            # a bottom margin/footer (many proceedings PDFs). Inspect individual
            # PDF lines/spans because a running head and its page label can share
            # one text block, which defeats block-level matching.
            for line in block.get("lines", []):
                line_text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
                if not re.fullmatch(r"\d{1,4}", line_text):
                    continue
                lx0, ly0, lx1, ly1 = (float(v) for v in line.get("bbox", (0, 0, 0, 0)))
                right_header = lx0 >= rect.width * 0.86 and ly1 <= rect.height * 0.10
                right_footer = lx0 >= rect.width * 0.78 and ly0 >= rect.height * 0.88
                centered_footer = rect.height - ly1 <= rect.height * 0.08 and rect.width * 0.40 <= lx0 <= rect.width * 0.60
                if right_header or right_footer or centered_footer:
                    page_numbers.append((page_no, int(line_text)))

            # Retain support for publisher footers where the page label is appended
            # to a copyright/DOI line rather than emitted as a separate line.
            if bbox[1] > rect.height * 0.88 and re.search(r"IEEE|DOI\s+10\.", compact_text, re.I):
                trailing = re.search(r"\s(\d{1,4})\s*$", compact_text)
                if trailing:
                    page_numbers.append((page_no, int(trailing.group(1))))

        # Detect a two-column layout by checking whether text blocks occupy both page halves.
        mid_x = rect.width / 2
        left = sum(1 for b in blocks if (b["bbox"][0] + b["bbox"][2]) / 2 < mid_x - 10)
        right = sum(1 for b in blocks if (b["bbox"][0] + b["bbox"][2]) / 2 > mid_x + 10)
        nonempty = max(1, left + right)
        two_col = left / nonempty > 0.20 and right / nonempty > 0.20

        page_summaries.append({
            "page": page_no,
            "width_pt": float(rect.width),
            "height_pt": float(rect.height),
            "two_columns_detected": two_col,
            "text_block_count": len(blocks),
            "text_block_bounds": [
                min((b["bbox"][0] for b in blocks), default=None),
                min((b["bbox"][1] for b in blocks), default=None),
                max((b["bbox"][2] for b in blocks), default=None),
                max((b["bbox"][3] for b in blocks), default=None),
            ],
        })

    full_text = "\n".join(all_text)
    return DocumentData(
        filename=filename,
        file_type="PDF",
        full_text=full_text,
        paragraphs=paragraphs,
        page_count=len(pdf),
        page_sizes_pt=page_sizes,
        images_count=0,  # PDF image accounting is not a reliable proxy for figure count.
        # A PDF's footer page number is not an editable Word PAGE field. Keep these
        # as separate evidence so publisher-added proceedings pagination is not a FAIL.
        has_page_number_field=False,
        has_extractable_text=bool(full_text.strip()),
        pdf_pages=page_summaries,
        parser_notes=["PDFs do not retain Word style names or editable section metadata. Layout and visual checks are therefore evidence-based heuristics."],
        pdf_page_numbers=list(dict.fromkeys(page_numbers)),
        pdf_publication_markers=publication_markers,
    )
