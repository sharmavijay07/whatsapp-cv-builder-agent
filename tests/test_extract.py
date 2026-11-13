import httpx
import pytest

from safu.errors import FetchError
from safu.extract import assert_public_url, extract_any, fetch_url, html_to_text
from safu.parse import parse_cv, parse_job
from safu.samples import CV_TEXT, JOB_HTML, OCR_TEXT, text_to_docx, text_to_image, text_to_pdf


def test_pdf_docx_and_ocr_round_trip():
    pdf_record = parse_cv(extract_any("cv.pdf", "application/pdf", text_to_pdf(CV_TEXT)))
    docx_record = parse_cv(
        extract_any(
            "cv.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            text_to_docx(CV_TEXT),
        )
    )
    ocr_record = parse_cv(extract_any("cv.png", "image/png", text_to_image(OCR_TEXT)))
    assert pdf_record.roles[0].employer == "SwiftHaul Logistics"
    assert docx_record.roles[1].employer == "Mombasa Road Traders"
    assert ocr_record.roles[0].employer == "SwiftHaul Logistics"
    assert ocr_record.confidence >= 0.45


def test_url_html_becomes_a_job_and_blocked_hosts_fail():
    job = parse_job(html_to_text(JOB_HTML))
    assert job.company == "Freightline Kenya"
    assert "excel" in job.keywords

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "jobs.example"
        return httpx.Response(200, text=JOB_HTML, headers={"content-type": "text/html"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    text = fetch_url("https://jobs.example/role", allow_local=False, client=client)
    assert "Freightline Kenya" in text

    with pytest.raises(FetchError):
        assert_public_url("http://169.254.169.254/latest/meta-data", allow_local=True)
    with pytest.raises(FetchError):
        assert_public_url("file:///etc/passwd", allow_local=True)
    with pytest.raises(FetchError):
        assert_public_url("http://127.0.0.1/secret", allow_local=False)
