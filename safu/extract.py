import io
import ipaddress
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from safu.errors import ExtractError, FetchError

MAX_URL_BYTES = 1_000_000


def extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = [(page.extract_text() or "") for page in reader.pages]
    text = "\n".join(pages).strip()
    if not text:
        raise ExtractError("empty_pdf")
    return text


def extract_docx(data: bytes) -> str:
    from docx import Document

    document = Document(io.BytesIO(data))
    lines = [paragraph.text for paragraph in document.paragraphs]
    text = "\n".join(lines).strip()
    if not text:
        raise ExtractError("empty_docx")
    return text


def extract_image(data: bytes) -> str:
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise ExtractError("ocr_unavailable") from exc
    try:
        image = Image.open(io.BytesIO(data))
        text = pytesseract.image_to_string(image, config="--psm 6")
    except pytesseract.TesseractNotFoundError as exc:
        raise ExtractError("ocr_unavailable") from exc
    except Exception as exc:
        raise ExtractError("ocr_failed") from exc
    text = text.strip()
    if len(text) < 8:
        raise ExtractError("ocr_failed")
    return text


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "title"]):
        tag.decompose()
    return soup.get_text("\n")


def extract_any(filename: str | None, content_type: str | None, data: bytes) -> str:
    name = (filename or "").lower()
    ctype = (content_type or "").lower()
    if name.endswith(".pdf") or "pdf" in ctype:
        return extract_pdf(data)
    if name.endswith(".docx") or "wordprocessingml" in ctype or name.endswith(".doc"):
        return extract_docx(data)
    if name.endswith(".txt") or ctype.startswith("text/plain"):
        return data.decode("utf-8", errors="replace")
    if name.endswith(".html") or "html" in ctype:
        return html_to_text(data.decode("utf-8", errors="replace"))
    if ctype.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff")):
        return extract_image(data)
    raise ExtractError("unsupported_type")


def assert_public_url(url: str, allow_local: bool) -> None:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise FetchError("bad_scheme")
    host = (parsed.hostname or "").lower()
    if not host:
        raise FetchError("bad_host")
    if host == "metadata.google.internal" or host.startswith("169.254."):
        raise FetchError("blocked_host")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        blocked = ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_loopback
        if blocked and not (allow_local and ip.is_loopback):
            raise FetchError("blocked_host")
    if host in {"localhost"} and not allow_local:
        raise FetchError("blocked_host")


def fetch_url(url: str, allow_local: bool, client: httpx.Client | None = None) -> str:
    assert_public_url(url, allow_local)
    own_client = client is None
    http = client or httpx.Client(timeout=10, follow_redirects=False)
    try:
        response = http.get(url, headers={"User-Agent": "SafuCV/1.0"})
    except httpx.HTTPError as exc:
        raise FetchError("fetch_failed") from exc
    finally:
        if own_client:
            http.close()
    if response.status_code >= 400:
        raise FetchError("fetch_failed")
    data = response.content[:MAX_URL_BYTES]
    ctype = response.headers.get("content-type", "")
    path = url.split("?", 1)[0].lower()
    if "pdf" in ctype or path.endswith(".pdf"):
        return extract_pdf(data)
    if "word" in ctype or path.endswith(".docx"):
        return extract_docx(data)
    if ctype.startswith("image/"):
        return extract_image(data)
    decoded = data.decode("utf-8", errors="replace")
    if "html" in ctype or decoded.lstrip()[:1] == "<":
        return html_to_text(decoded)
    return decoded
