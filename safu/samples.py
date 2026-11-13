import io

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph, SimpleDocTemplate

CV_TEXT = """Amina Wanjiku
Nairobi, Kenya
amina.wanjiku@email.com
+254 712 345 678

PROFESSIONAL SUMMARY
Operations coordinator with 4 years in dispatch and warehouse teams in Nairobi. Comfortable with Excel, inventory counts, rider schedules, and customer updates. Previously a warehouse assistant handling outbound orders and Friday stock counts.

SKILLS
Excel, Inventory control, Dispatch scheduling, Customer updates, Google Sheets, Record keeping

EXPERIENCE
Operations Coordinator
SwiftHaul Logistics, Nairobi
March 2021 - Present
- Cut dispatch errors from 22 a week to 18 a week by checking manifests before riders left.
- Scheduled a team of 6 riders across two shifts.
- Kept on-time delivery at 91% for assigned routes in 2024.

Warehouse Assistant
Mombasa Road Traders, Nairobi
June 2018 - February 2021
- Counted stock for 120 SKUs each Friday and logged variances.
- Prepared 40 outbound orders a day during peak months.

EDUCATION
Diploma in Supply Chain Management
Kenya Institute of Management, Nairobi
2016 - 2018
"""

JOB_TEXT = """Logistics Coordinator
Freightline Kenya - Nairobi

We are hiring a Logistics Coordinator to own dispatch, carrier follow-up, and inventory accuracy for our Nairobi hub.

Requirements
- 3 years in logistics, warehouse, or transport operations
- Strong Excel and Google Sheets
- Inventory control and cycle counts
- Clear written updates to customers and drivers
- Diploma or degree in supply chain, business, or a related field

Responsibilities
- Build daily dispatch plans
- Track on-time delivery and report exceptions
- Reconcile stock variances with the warehouse team
"""

JOB_HTML = """<!doctype html>
<html lang="en">
<head><title>Logistics Coordinator</title></head>
<body>
<h1>Logistics Coordinator</h1>
<p>Freightline Kenya - Nairobi</p>
<p>We are hiring a Logistics Coordinator to own dispatch, carrier follow-up, and inventory accuracy for our Nairobi hub.</p>
<h2>Requirements</h2>
<ul>
<li>3 years in logistics, warehouse, or transport operations</li>
<li>Strong Excel and Google Sheets</li>
<li>Inventory control and cycle counts</li>
<li>Clear written updates to customers and drivers</li>
<li>Diploma or degree in supply chain, business, or a related field</li>
</ul>
<h2>Responsibilities</h2>
<ul>
<li>Build daily dispatch plans</li>
<li>Track on-time delivery and report exceptions</li>
<li>Reconcile stock variances with the warehouse team</li>
</ul>
</body>
</html>
"""

OCR_TEXT = """Amina Wanjiku
Nairobi, Kenya
amina.wanjiku@email.com
+254 712 345 678

EXPERIENCE
Operations Coordinator
SwiftHaul Logistics
March 2021 - Present
- Scheduled a team of 6 riders across two shifts.

EDUCATION
Diploma in Supply Chain Management
Kenya Institute of Management
2016 - 2018
"""


def _xml(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def text_to_pdf(text: str) -> bytes:
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=36,
    )
    style = ParagraphStyle("line", fontName="Helvetica", fontSize=10, leading=13)
    story = [Paragraph(_xml(line) or "&nbsp;", style) for line in text.splitlines()]
    document.build(story)
    return buffer.getvalue()


def text_to_docx(text: str) -> bytes:
    from docx import Document

    document = Document()
    for line in text.splitlines():
        document.add_paragraph(line)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def text_to_image(text: str) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    lines = text.splitlines() or [""]
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    try:
        font = ImageFont.truetype(font_path, 36)
    except OSError:
        font = ImageFont.load_default()
    padding = 40
    line_height = 48
    width = 1400
    height = padding * 2 + line_height * max(len(lines), 1)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.text((padding, padding + index * line_height), line, fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
