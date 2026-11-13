import io

from docx import Document
from docx.enum.text import WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from safu.models import CVContent, InterviewContent

INK = RGBColor(0x1C, 0x19, 0x15)
GREEN = RGBColor(0x0F, 0x3D, 0x32)


def _xml(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _shade_heading(paragraph) -> None:
    properties = paragraph._p.get_or_add_pPr()
    border = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "0F3D32")
    border.append(bottom)
    properties.append(border)


def _run(paragraph, text: str, size: int, bold: bool = False, color: RGBColor = INK):
    run = paragraph.add_run(text)
    run.font.name = "Calibri"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "Calibri")
    run.font.size = Pt(size)
    run.bold = bold
    run.font.color.rgb = color
    return run


def _heading(document: Document, text: str) -> None:
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(12)
    paragraph.paragraph_format.space_after = Pt(4)
    _shade_heading(paragraph)
    _run(paragraph, text.upper(), 12, bold=True, color=GREEN)


def _body(document: Document, text: str, *, bullet: bool = False, bold: bool = False, size: int = 11):
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_after = Pt(2)
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
    if bullet:
        paragraph.paragraph_format.left_indent = Inches(0.2)
        text = "- " + text
    _run(paragraph, text, size, bold=bold)
    return paragraph


def _prepare(document: Document) -> None:
    section = document.sections[0]
    section.page_width = Inches(8.27)
    section.page_height = Inches(11.69)
    section.top_margin = Inches(0.7)
    section.bottom_margin = Inches(0.7)
    section.left_margin = Inches(0.8)
    section.right_margin = Inches(0.8)
    document.core_properties.author = "Safu"
    document.core_properties.comments = ""


def render_cv_docx(cv: CVContent) -> bytes:
    document = Document()
    _prepare(document)
    name = document.add_paragraph()
    name.paragraph_format.space_after = Pt(2)
    _run(name, cv.name, 18, bold=True)
    contact = "  |  ".join(part for part in (cv.location, cv.email, cv.phone) if part)
    _body(document, contact, size=10)
    _heading(document, "Professional summary")
    _body(document, cv.summary)
    _heading(document, "Skills")
    _body(document, ", ".join(cv.skills))
    _heading(document, "Experience")
    for role in cv.roles:
        _body(document, role.title, bold=True)
        _body(document, f"{role.employer}  |  {role.start} - {role.end}")
        for bullet in role.bullets:
            _body(document, bullet, bullet=True)
    if cv.education:
        _heading(document, "Education")
        for edu in cv.education:
            _body(document, edu.qualification, bold=True)
            _body(document, f"{edu.institution}  |  {edu.start} - {edu.end}")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def render_prose_docx(title: str, blocks: list[tuple[str, list[str]]]) -> bytes:
    document = Document()
    _prepare(document)
    heading = document.add_paragraph()
    _run(heading, title, 16, bold=True)
    for label, lines in blocks:
        if label:
            _heading(document, label)
        for line in lines:
            _body(document, line)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _pdf_styles():
    return {
        "name": ParagraphStyle("name", fontName="Helvetica-Bold", fontSize=16, leading=20, textColor=HexColor("#1C1915")),
        "contact": ParagraphStyle("contact", fontName="Helvetica", fontSize=9, leading=12, textColor=HexColor("#333333")),
        "h": ParagraphStyle(
            "h",
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=14,
            textColor=HexColor("#0F3D32"),
            spaceBefore=10,
            spaceAfter=3,
        ),
        "body": ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=13, textColor=HexColor("#1C1915")),
        "bullet": ParagraphStyle(
            "bullet",
            fontName="Helvetica",
            fontSize=10,
            leading=13,
            leftIndent=12,
            textColor=HexColor("#1C1915"),
        ),
    }


def _pdf(story: list) -> bytes:
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=46,
        rightMargin=46,
        topMargin=46,
        bottomMargin=46,
        title="Safu document",
        author="Safu",
    )
    document.build(story)
    return buffer.getvalue()


def render_cv_pdf(cv: CVContent) -> bytes:
    styles = _pdf_styles()
    story = [Paragraph(_xml(cv.name), styles["name"])]
    contact = "  |  ".join(part for part in (cv.location, cv.email, cv.phone) if part)
    story.append(Paragraph(_xml(contact), styles["contact"]))
    story.append(Paragraph("PROFESSIONAL SUMMARY", styles["h"]))
    story.append(Paragraph(_xml(cv.summary), styles["body"]))
    story.append(Paragraph("SKILLS", styles["h"]))
    story.append(Paragraph(_xml(", ".join(cv.skills)), styles["body"]))
    story.append(Paragraph("EXPERIENCE", styles["h"]))
    for role in cv.roles:
        story.append(Paragraph(_xml(role.title), styles["body"]))
        story.append(Paragraph(_xml(f"{role.employer}  |  {role.start} - {role.end}"), styles["body"]))
        for bullet in role.bullets:
            story.append(Paragraph(_xml("- " + bullet), styles["bullet"]))
    if cv.education:
        story.append(Paragraph("EDUCATION", styles["h"]))
        for edu in cv.education:
            story.append(Paragraph(_xml(edu.qualification), styles["body"]))
            story.append(Paragraph(_xml(f"{edu.institution}  |  {edu.start} - {edu.end}"), styles["body"]))
    story.append(Spacer(1, 8))
    return _pdf(story)


def render_blocks_pdf(title: str, blocks: list[tuple[str, list[str]]]) -> bytes:
    styles = _pdf_styles()
    story = [Paragraph(_xml(title), styles["name"])]
    for label, lines in blocks:
        if label:
            story.append(Paragraph(_xml(label.upper()), styles["h"]))
        for line in lines:
            story.append(Paragraph(_xml(line) or "&nbsp;", styles["body"]))
    return _pdf(story)


def cover_blocks(letter: str) -> list[tuple[str, list[str]]]:
    paragraphs = [part.strip() for part in letter.split("\n\n") if part.strip()]
    if not paragraphs:
        paragraphs = [line.strip() for line in letter.splitlines() if line.strip()]
    return [("", paragraphs)]


def interview_blocks(interview: InterviewContent) -> list[tuple[str, list[str]]]:
    return [
        ("From the job post", interview.posting or ["No excerpt was available."]),
        ("Matching evidence from your CV", interview.evidence or ["No overlapping bullet was found."]),
        ("Gaps to prepare for", interview.gaps or ["No keyword gaps were found."]),
        ("Questions they may ask", interview.questions),
        ("Questions to ask", interview.ask_them),
        ("60-second introduction", [interview.intro]),
    ]
