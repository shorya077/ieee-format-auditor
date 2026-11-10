from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Finding:
    rule_id: str
    category: str
    status: str  # PASS, FAIL, REVIEW, INFO
    title: str
    message: str
    location: str = "Document"
    recommendation: str = ""
    evidence: str = ""
    confidence: str = "high"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Paragraph:
    index: int
    text: str
    style_id: str = ""
    style_name: str = ""
    page: int | None = None
    fonts: list[str] = field(default_factory=list)
    font_sizes_pt: list[float] = field(default_factory=list)
    has_math: bool = False
    has_footnote: bool = False
    is_bold: bool = False
    is_italic: bool = False
    alignment: str = ""
    has_image: bool = False
    numbering_format: str = ""
    numbering_text: str = ""
    numbering_num_id: str = ""
    bbox_pt: tuple[float, float, float, float] | None = None


@dataclass
class DocumentData:
    filename: str
    file_type: str
    full_text: str
    paragraphs: list[Paragraph] = field(default_factory=list)
    page_count: int = 0
    page_sizes_pt: list[tuple[float, float]] = field(default_factory=list)
    margins_pt: dict[str, float] = field(default_factory=dict)
    section_columns: list[int] = field(default_factory=list)
    tables_count: int = 0
    images_count: int = 0
    has_page_number_field: bool = False
    has_extractable_text: bool = True
    pdf_pages: list[dict[str, Any]] = field(default_factory=list)
    parser_notes: list[str] = field(default_factory=list)
    pdf_page_numbers: list[tuple[int, int]] = field(default_factory=list)
    pdf_publication_markers: list[str] = field(default_factory=list)
    header_footer_text: list[str] = field(default_factory=list)
