import re

from safu.models import Education, JobPost, Role, SourceRecord
from safu.scores import extract_keywords

HEADINGS = {
    "professional summary": "summary",
    "summary": "summary",
    "profile": "summary",
    "about": "summary",
    "skills": "skills",
    "core skills": "skills",
    "technical skills": "skills",
    "key skills": "skills",
    "experience": "experience",
    "work experience": "experience",
    "professional experience": "experience",
    "employment history": "experience",
    "employment": "experience",
    "education": "education",
    "academic background": "education",
    "qualifications": "education",
}

_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_POINT = rf"(?:{_MONTH}\s+\d{{4}}|\d{{1,2}}/\d{{4}}|\d{{4}})"
_END = rf"(?:present|current|now|{_POINT})"
RANGE_RE = re.compile(rf"({_POINT})\s*(?:to|–|—|-)\s*({_END})", re.IGNORECASE)
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
PHONE_RE = re.compile(r"\+\d{1,3}(?:[\s\-]\d{2,4}){2,4}|(?<!\d)0\d{3}[\s\-]?\d{3}[\s\-]?\d{3}(?!\d)")
BULLET_RE = re.compile(r"^\s*(?:[-•*–]|\d+[.)])\s+\S")


def heading_key(line: str) -> str | None:
    if len(line.strip()) > 40:
        return None
    cleaned = re.sub(r"[^a-z ]+", "", line.strip().casefold())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return HEADINGS.get(cleaned)


def looks_like_name(line: str) -> bool:
    parts = line.strip().split()
    if not 2 <= len(parts) <= 4:
        return False
    return all(re.fullmatch(r"[A-Za-z][A-Za-z'’-]*", part) for part in parts)


def clean_org(line: str) -> str:
    line = line.split("|")[0].strip()
    if "," in line:
        left, right = line.split(",", 1)
        if right.strip() and len(right.strip()) < 30:
            return left.strip()
    return line.strip()


def strip_bullet(line: str) -> str:
    return re.sub(r"^\s*(?:[-•*–]|\d+[.)])\s+", "", line).strip()


def _sections(text: str) -> dict[str, list[str]]:
    sections = {key: [] for key in ("header", "summary", "skills", "experience", "education")}
    current = "header"
    for raw in text.splitlines():
        key = heading_key(raw)
        if key:
            current = key
            continue
        sections[current].append(raw.rstrip())
    return sections


def _dated_blocks(lines: list[str]) -> list[tuple[list[str], str, str, list[str]]]:
    blocks = []
    buffer: list[str] = []
    index = 0
    usable = [line.strip() for line in lines]
    while index < len(usable):
        line = usable[index]
        match = RANGE_RE.search(line) if line else None
        if match and not BULLET_RE.match(line):
            start = re.sub(r"\s+", " ", match.group(1)).strip()
            end = re.sub(r"\s+", " ", match.group(2)).strip()
            bullets: list[str] = []
            index += 1
            while index < len(usable) and usable[index] and BULLET_RE.match(usable[index]):
                bullets.append(strip_bullet(usable[index]))
                index += 1
            blocks.append((buffer, start, end, bullets))
            buffer = []
            continue
        if line:
            buffer.append(line)
        index += 1
    return blocks


def _skills(lines: list[str]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for line in lines:
        cleaned = strip_bullet(line).strip()
        if not cleaned:
            continue
        for part in re.split(r"[,;|•]", cleaned):
            skill = part.strip(" .")
            key = skill.casefold()
            if skill and key not in seen and len(skill) < 60:
                seen.add(key)
                found.append(skill)
    return found


def parse_cv(text: str) -> SourceRecord:
    sections = _sections(text)
    email_match = EMAIL_RE.search(text)
    phone_match = PHONE_RE.search(text)
    email = email_match.group(0) if email_match else None
    phone = re.sub(r"\s+", " ", phone_match.group(0)).strip() if phone_match else None
    name = None
    location = None
    for line in sections["header"]:
        stripped = line.strip(" |")
        if not stripped or EMAIL_RE.search(stripped) or PHONE_RE.search(stripped):
            continue
        if name is None and looks_like_name(stripped):
            name = stripped
            continue
        if location is None and "," in stripped and len(stripped) < 60:
            location = stripped
    summary = " ".join(line.strip() for line in sections["summary"] if line.strip())
    roles: list[Role] = []
    for buffer, start, end, bullets in _dated_blocks(sections["experience"]):
        title = buffer[0] if buffer else ""
        employer_line = buffer[1] if len(buffer) > 1 else (buffer[0] if buffer else "")
        roles.append(
            Role(
                title=title,
                employer=clean_org(employer_line),
                start=start,
                end=end,
                bullets=bullets,
            )
        )
    education: list[Education] = []
    for buffer, start, end, _bullets in _dated_blocks(sections["education"]):
        qualification = buffer[0] if buffer else ""
        institution = clean_org(buffer[1] if len(buffer) > 1 else (buffer[0] if buffer else ""))
        if qualification:
            education.append(
                Education(
                    qualification=qualification,
                    institution=institution,
                    start=start,
                    end=end,
                )
            )
    skills = _skills(sections["skills"])
    record = SourceRecord(
        name=name,
        email=email,
        phone=phone,
        location=location,
        summary=summary,
        skills=skills,
        roles=roles,
        education=education,
        raw_text=text,
        confidence=0.0,
    )
    record.confidence = _confidence(record)
    return record


def _confidence(record: SourceRecord) -> float:
    score = 0.0
    if record.roles:
        score += 0.5
    if record.roles and all(role.employer and role.start and role.end for role in record.roles):
        score += 0.15
    if record.education:
        score += 0.15
    if record.email or record.phone:
        score += 0.1
    if record.skills:
        score += 0.05
    if len(record.raw_text) > 200:
        score += 0.05
    return min(score, 1.0)


def parse_job(text: str) -> JobPost:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    title = lines[0][:80] if lines else None
    company = None
    location = None
    if len(lines) > 1 and len(lines[1]) < 80 and not lines[1].lower().startswith("we "):
        parts = re.split(r"\s+[—–|-]\s+|\s+-\s+", lines[1], maxsplit=1)
        company = parts[0].strip()
        if len(parts) > 1:
            location = parts[1].strip()
    requirements: list[str] = []
    capture = False
    for line in lines:
        key = heading_key(line) or line.casefold().rstrip(":")
        if key in {"requirements", "responsibilities", "requirement", "responsibility"}:
            capture = True
            continue
        if capture and BULLET_RE.match(line):
            requirements.append(strip_bullet(line))
        elif capture and heading_key(line):
            capture = False
    return JobPost(
        title=title,
        company=company,
        location=location,
        raw_text=text,
        keywords=extract_keywords(text),
        requirements=requirements,
    )
