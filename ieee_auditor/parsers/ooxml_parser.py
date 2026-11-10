"""Read DOCX packages directly; supports Transitional and Strict OOXML namespaces."""
from __future__ import annotations

import re
import zipfile
from pathlib import PurePosixPath
from lxml import etree

from ieee_auditor.models import DocumentData, Paragraph


def local_name(node: etree._Element) -> str:
    return etree.QName(node).localname


def first_child(node: etree._Element, name: str):
    found = node.xpath(f'./*[local-name()="{name}"]')
    return found[0] if found else None


def attr_local(node: etree._Element | None, name: str, default=None):
    if node is None:
        return default
    for key, value in node.attrib.items():
        if etree.QName(key).localname == name:
            return value
    return default


def safe_xml(data: bytes):
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=True, huge_tree=False)
    return etree.fromstring(data, parser=parser)


def _resolve_target(base_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return str(PurePosixPath(base_part).parent.joinpath(target))


def _extract_relationships(zf: zipfile.ZipFile) -> dict[str, str]:
    try:
        rels = safe_xml(zf.read("_rels/.rels"))
    except KeyError:
        return {}
    for rel in rels:
        if local_name(rel) == "Relationship" and attr_local(rel, "Type", "").endswith("/officeDocument"):
            target = attr_local(rel, "Target", "word/document.xml")
            main_part = _resolve_target("", target)
            break
    else:
        main_part = "word/document.xml"
    rels_part = str(PurePosixPath(main_part).parent / "_rels" / (PurePosixPath(main_part).name + ".rels"))
    result: dict[str, str] = {}
    try:
        root = safe_xml(zf.read(rels_part))
        for rel in root:
            if local_name(rel) == "Relationship":
                rid = attr_local(rel, "Id")
                target = attr_local(rel, "Target", "")
                if rid and target:
                    result[rid] = _resolve_target(main_part, target)
    except KeyError:
        pass
    return result


def _text_from_paragraph(p: etree._Element) -> str:
    pieces: list[str] = []
    for node in p.iter():
        name = local_name(node)
        if name in {"t", "delText", "instrText"} and node.text:
            pieces.append(node.text)
        elif name == "tab":
            pieces.append("\t")
        elif name in {"br", "cr"}:
            pieces.append("\n")
    return "".join(pieces).strip()


def _style_data(style_root: etree._Element) -> dict[str, dict]:
    styles = {}
    for s in style_root.xpath('//*[local-name()="style"]'):
        sid = attr_local(s, "styleId", "")
        name_el = first_child(s, "name")
        name = attr_local(name_el, "val", sid)
        rpr = first_child(s, "rPr")
        ppr = first_child(s, "pPr")
        size_el = first_child(rpr, "sz") if rpr is not None else None
        fonts_el = first_child(rpr, "rFonts") if rpr is not None else None
        fonts = []
        if fonts_el is not None:
            for k, v in fonts_el.attrib.items():
                if etree.QName(k).localname in {"ascii", "hAnsi", "eastAsia", "cs"} and v not in fonts:
                    fonts.append(v)
        num_pr = first_child(ppr, "numPr") if ppr is not None else None
        styles[sid] = {
            "name": name,
            "based_on": attr_local(first_child(s, "basedOn"), "val", "") if s is not None else "",
            "num_id": attr_local(first_child(num_pr, "numId"), "val", "") if num_pr is not None else "",
            "ilvl": attr_local(first_child(num_pr, "ilvl"), "val", "0") if num_pr is not None else "0",
            "size_pt": float(attr_local(size_el, "val")) / 2 if size_el is not None and attr_local(size_el, "val", "").isdigit() else None,
            "fonts": fonts,
            "bold": first_child(rpr, "b") is not None if rpr is not None else False,
            "italic": first_child(rpr, "i") is not None if rpr is not None else False,
            "alignment": attr_local(first_child(ppr, "jc"), "val", "") if ppr is not None else "",
        }
    return styles


def _effective_style(style_id: str, styles: dict[str, dict]) -> dict:
    merged = {"name": style_id, "size_pt": None, "fonts": [], "bold": False, "italic": False, "alignment": "", "num_id": "", "ilvl": "0"}
    if style_id in styles:
        merged["name"] = styles[style_id].get("name", style_id)
    seen = set()
    current = style_id
    while current and current not in seen and current in styles:
        seen.add(current)
        st = styles[current]
        for key in ("size_pt", "bold", "italic", "alignment"):
            if merged.get(key) in (None, False, "") and st.get(key) not in (None, False, ""):
                merged[key] = st[key]
        if not merged["fonts"] and st.get("fonts"):
            merged["fonts"] = st["fonts"]
        if not merged["num_id"] and st.get("num_id"):
            merged["num_id"] = st["num_id"]
            merged["ilvl"] = st.get("ilvl", "0")
        current = st.get("based_on", "")
    return merged


def _numbering_map(zf: zipfile.ZipFile) -> dict[str, dict[str, str]]:
    """Map numId to the visible level-zero numbering pattern and format."""
    result: dict[str, dict[str, str]] = {}
    try:
        root = safe_xml(zf.read("word/numbering.xml"))
    except KeyError:
        return result
    abstract = {}
    for a in root.xpath('//*[local-name()="abstractNum"]'):
        aid = attr_local(a, "abstractNumId", "")
        lvl = a.xpath('./*[local-name()="lvl" and @*[local-name()="ilvl"]="0"]')
        if not lvl:
            lvl = a.xpath('./*[local-name()="lvl"]')[:1]
        if aid and lvl:
            fmt = first_child(lvl[0], "numFmt")
            txt = first_child(lvl[0], "lvlText")
            abstract[aid] = {"format": attr_local(fmt, "val", ""), "text": attr_local(txt, "val", "")}
    for num in root.xpath('//*[local-name()="num"]'):
        nid = attr_local(num, "numId", "")
        aid = attr_local(first_child(num, "abstractNumId"), "val", "")
        if nid and aid in abstract:
            result[nid] = abstract[aid]
    return result


def parse_docx(data: bytes, filename: str) -> DocumentData:
    try:
        zf = zipfile.ZipFile(__import__("io").BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("The uploaded file is not a valid DOCX package.") from exc

    names = set(zf.namelist())
    if "word/document.xml" not in names:
        # Strict OOXML packages can still use the conventional part path; resolve alternate main part if necessary.
        rels_root = safe_xml(zf.read("_rels/.rels"))
        target = None
        for rel in rels_root:
            if local_name(rel) == "Relationship" and attr_local(rel, "Type", "").endswith("/officeDocument"):
                target = _resolve_target("", attr_local(rel, "Target", ""))
                break
        if not target or target not in names:
            raise ValueError("Could not locate the Word document part inside this DOCX package.")
        document_part = target
    else:
        document_part = "word/document.xml"

    doc_root = safe_xml(zf.read(document_part))
    style_part = str(PurePosixPath(document_part).parent / "styles.xml")
    style_map = {}
    if style_part in names:
        style_map = _style_data(safe_xml(zf.read(style_part)))
    numbering_map = _numbering_map(zf)

    paragraphs: list[Paragraph] = []
    blocks = doc_root.xpath('//*[local-name()="body"]/*')
    table_count = 0
    image_count = 0
    for node in blocks:
        kind = local_name(node)
        if kind == "tbl":
            table_count += 1
            # Table-contained paragraphs are still useful to text and caption checks.
            p_nodes = node.xpath('.//*[local-name()="p"]')
        elif kind == "p":
            p_nodes = [node]
        else:
            continue
        for p in p_nodes:
            text = _text_from_paragraph(p)
            ppr = first_child(p, "pPr")
            style_el = first_child(ppr, "pStyle") if ppr is not None else None
            style_id = attr_local(style_el, "val", "")
            st = _effective_style(style_id, style_map)
            fonts = list(st.get("fonts", []))
            sizes = [st["size_pt"]] if st.get("size_pt") else []
            bold = bool(st.get("bold"))
            italic = bool(st.get("italic"))
            for r in p.xpath('.//*[local-name()="r"]'):
                rpr = first_child(r, "rPr")
                if rpr is None:
                    continue
                sz = first_child(rpr, "sz")
                val = attr_local(sz, "val", "")
                if val:
                    try:
                        sizes.append(float(val) / 2)
                    except ValueError:
                        pass
                rf = first_child(rpr, "rFonts")
                if rf is not None:
                    for k, v in rf.attrib.items():
                        if etree.QName(k).localname in {"ascii", "hAnsi", "eastAsia", "cs"} and v not in fonts:
                            fonts.append(v)
                bold = bold or first_child(rpr, "b") is not None
                italic = italic or first_child(rpr, "i") is not None
            image_here = bool(p.xpath('.//*[local-name()="drawing" or local-name()="pict"]'))
            image_count += len(p.xpath('.//*[local-name()="drawing" or local-name()="pict"]'))
            math_here = bool(p.xpath('.//*[local-name()="oMath" or local-name()="oMathPara"]'))
            footnote_here = bool(p.xpath('.//*[local-name()="footnoteReference" or local-name()="endnoteReference"]'))
            alignment = st.get("alignment", "")
            if ppr is not None:
                jc = first_child(ppr, "jc")
                alignment = attr_local(jc, "val", alignment)
            num_pr = first_child(ppr, "numPr") if ppr is not None else None
            num_id = attr_local(first_child(num_pr, "numId"), "val", "") if num_pr is not None else st.get("num_id", "")
            numbering = numbering_map.get(num_id, {})
            paragraphs.append(Paragraph(
                index=len(paragraphs) + 1,
                text=text,
                style_id=style_id,
                style_name=st.get("name", style_id),
                fonts=fonts,
                font_sizes_pt=sorted(set(round(x, 2) for x in sizes)),
                has_math=math_here,
                has_footnote=footnote_here,
                is_bold=bold,
                is_italic=italic,
                alignment=alignment,
                has_image=image_here,
                numbering_format=numbering.get("format", ""),
                numbering_text=numbering.get("text", ""),
                numbering_num_id=num_id,
            ))

    sections = []
    for sec in doc_root.xpath('//*[local-name()="sectPr"]'):
        pg = first_child(sec, "pgSz")
        mar = first_child(sec, "pgMar")
        cols = first_child(sec, "cols")
        def as_float(el, key):
            raw = attr_local(el, key, "")
            if not raw:
                return None
            match = __import__("re").match(r"^([0-9]+(?:\.[0-9]+)?)(pt)?$", str(raw))
            if not match:
                return None
            value = float(match.group(1))
            # OOXML stores section lengths as twips unless an explicit unit is
            # present. Convert all unitless values (including margins such as
            # 1008 twips) to points. Some Strict OOXML templates store values
            # with explicit point strings (e.g. 612pt).
            if not match.group(2):
                value /= 20.0
            return value
        if pg is not None:
            sections.append({
                "width_pt": as_float(pg, "w"),
                "height_pt": as_float(pg, "h"),
                "top": as_float(mar, "top"), "right": as_float(mar, "right"),
                "bottom": as_float(mar, "bottom"), "left": as_float(mar, "left"),
                "columns": int(attr_local(cols, "num", "1") or 1),
            })

    # Use the most frequent section settings; mixed section layouts are retained for diagnostics.
    margins = {}
    if sections:
        for side in ("top", "right", "bottom", "left"):
            vals = [s[side] for s in sections if s.get(side) is not None]
            if vals:
                margins[side] = sorted(vals)[len(vals) // 2]
    page_sizes = [(s["width_pt"], s["height_pt"]) for s in sections if s.get("width_pt") and s.get("height_pt")]
    columns = [s["columns"] for s in sections]

    # Header/footer text is important for manuscript-ID placeholders and page
    # fields, but is kept separate from body paragraphs so it does not distort
    # heading, reference, and acronym analysis.
    page_num = False
    header_footer_text: list[str] = []
    for part in names:
        if re.match(r"word/(header|footer)\d*\.xml$", part):
            root = safe_xml(zf.read(part))
            for p in root.xpath('.//*[local-name()="p"]'):
                value = _text_from_paragraph(p)
                if value:
                    header_footer_text.append(value)
            if root.xpath('.//*[local-name()="instrText" and contains(translate(text(), "page", "PAGE"), "PAGE")]'):
                page_num = True
            if root.xpath('.//*[local-name()="fldChar"]') and "PAGE" in " ".join(root.itertext()).upper():
                page_num = True

    text = "\n".join(p.text for p in paragraphs if p.text)
    notes = []
    if any(c not in (1, 2) for c in columns):
        notes.append("The DOCX contains multi-section layout settings; verify the rendered page layout.")
    if not paragraphs:
        notes.append("No paragraph text was extracted from the DOCX.")
    return DocumentData(
        filename=filename,
        file_type="DOCX",
        full_text=text,
        paragraphs=paragraphs,
        page_count=0,  # DOCX pagination is renderer-dependent.
        page_sizes_pt=page_sizes,
        margins_pt=margins,
        section_columns=columns,
        tables_count=table_count,
        images_count=image_count,
        has_page_number_field=page_num,
        has_extractable_text=bool(text.strip()),
        parser_notes=notes,
        header_footer_text=header_footer_text,
    )
