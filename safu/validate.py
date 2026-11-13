import re
from dataclasses import dataclass

from safu.models import CVContent, Education, InterviewContent, JobPost, Role, SourceRecord
from safu.parse import RANGE_RE

NUM_RE = re.compile(r"\d+(?:\.\d+)?%?")
PROPER_RE = re.compile(r"\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){1,4})\b")
DEGREE_RE = re.compile(
    r"\b(MBA|PhD|BSc|MSc|BA|MA|Bachelor(?:'s)?|Master(?:'s)?|Diploma|Degree|Certificate)\b",
    re.IGNORECASE,
)
ALLOWED_PHRASES = {"Hiring Manager", "Dear Hiring Manager"}


@dataclass(frozen=True)
class Violation:
    code: str
    detail: str


def norm(value: str) -> str:
    value = value.casefold().replace("–", "-").replace("—", "-")
    value = re.sub(r"[^a-z0-9% ]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _month(token: str) -> int:
    word = re.sub(r"[^a-z]", "", token.casefold())[:3]
    return {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }.get(word, 0)


def date_key(value: str) -> tuple[int, int, bool]:
    text = value.strip()
    if text.casefold() in {"present", "current", "now"}:
        return (9999, 0, True)
    month_match = re.match(
        r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
        r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(\d{4})",
        text,
        re.IGNORECASE,
    )
    if month_match:
        return (int(month_match.group(2)), _month(month_match.group(1)), False)
    year_match = re.search(r"(\d{4})", text)
    if year_match:
        return (int(year_match.group(1)), 0, False)
    return (0, 0, False)


def dates_compatible(draft_start: str, draft_end: str, source_start: str, source_end: str) -> bool:
    left = (date_key(draft_start), date_key(draft_end))
    right = (date_key(source_start), date_key(source_end))
    for draft, source in zip(left, right):
        if draft[2] != source[2]:
            return False
        if draft[0] != source[0]:
            return False
        if draft[1] and source[1] and draft[1] != source[1]:
            return False
        if draft[1] and not source[1]:
            return False
    return True


def _org_match(candidate: str, allowed: str) -> bool:
    left = norm(candidate)
    right = norm(allowed)
    if not left or not right:
        return False
    if left == right or left in right or right in left:
        return True
    return False


def _match_role(role: Role, source_roles: list[Role]) -> Role | None:
    for source in source_roles:
        if _org_match(role.employer, source.employer):
            return source
    return None


def _numbers(text: str) -> set[str]:
    return {match.lower() for match in NUM_RE.findall(text)}


def _strip_contacts(text: str, record: SourceRecord, cv: CVContent | None = None) -> str:
    values = [record.email, record.phone, record.name]
    if cv is not None:
        values.extend([cv.email, cv.phone, cv.name])
    for value in values:
        if value:
            text = text.replace(value, " ")
            digits = re.sub(r"\D", "", value)
            if len(digits) >= 7:
                text = re.sub(r"\+?" + re.escape(digits), " ", text)
    return text


def _source_numbers(record: SourceRecord) -> set[str]:
    return _numbers(_strip_contacts(record.raw_text, record))


def validate_cv(record: SourceRecord, cv: CVContent) -> list[Violation]:
    violations: list[Violation] = []
    used: set[int] = set()
    if len(cv.roles) != len(record.roles):
        violations.append(Violation("employer", "role count differs from the source record"))
    for role in cv.roles:
        source = _match_role(role, record.roles)
        if source is None or id(source) in used:
            violations.append(Violation("employer", f"employer not in source: {role.employer}"))
            if not any(
                dates_compatible(role.start, role.end, item.start, item.end) for item in record.roles
            ):
                violations.append(Violation("date", f"dates not in source: {role.start} - {role.end}"))
            continue
        used.add(id(source))
        if not dates_compatible(role.start, role.end, source.start, source.end):
            violations.append(Violation("date", f"dates changed for {role.employer}"))
    used_edu: set[int] = set()
    if len(cv.education) != len(record.education):
        violations.append(Violation("qualification", "education count differs from the source record"))
    for edu in cv.education:
        source = _match_edu(edu, record.education)
        if source is None or id(source) in used_edu:
            violations.append(Violation("qualification", f"qualification not in source: {edu.qualification}"))
            if not any(dates_compatible(edu.start, edu.end, item.start, item.end) for item in record.education):
                violations.append(Violation("date", f"education dates not in source: {edu.start} - {edu.end}"))
            continue
        used_edu.add(id(source))
        if not dates_compatible(edu.start, edu.end, source.start, source.end):
            violations.append(Violation("date", f"education dates changed for {edu.institution}"))
    for skill in cv.skills:
        if norm(skill) not in norm(record.raw_text):
            violations.append(Violation("skill", f"skill not in source: {skill}"))
    body = " ".join(
        [cv.summary, " ".join(cv.skills)]
        + [bullet for role in cv.roles for bullet in role.bullets]
        + [f"{edu.qualification} {edu.institution}" for edu in cv.education]
    )
    allowed = _source_numbers(record)
    extra = _numbers(_strip_contacts(body, record, cv)) - allowed
    for number in sorted(extra):
        violations.append(Violation("figure", f"figure not in source: {number}"))
    return violations


def _match_edu(edu: Education, source_items: list[Education]) -> Education | None:
    for source in source_items:
        qual_ok = norm(edu.qualification) in norm(source.qualification) or norm(source.qualification) in norm(
            edu.qualification
        )
        inst_ok = _org_match(edu.institution, source.institution)
        if qual_ok and inst_ok:
            return source
    return None


def _allowed_blob(record: SourceRecord, job: JobPost | None, extra: str = "") -> str:
    parts = [record.raw_text, extra, "Hiring Manager", "Dear Hiring Manager"]
    if job is not None:
        parts.append(job.raw_text)
        parts.extend([job.title or "", job.company or "", job.location or ""])
    return norm(" ".join(parts))


def validate_prose(record: SourceRecord, job: JobPost | None, text: str, extra_allowed: str = "") -> list[Violation]:
    violations: list[Violation] = []
    allowed_numbers = _source_numbers(record)
    extra_numbers = _numbers(_strip_contacts(text, record)) - allowed_numbers
    for number in sorted(extra_numbers):
        violations.append(Violation("figure", f"figure not in source: {number}"))
    blob = _allowed_blob(record, job, extra_allowed)
    for phrase in PROPER_RE.findall(text):
        if phrase in ALLOWED_PHRASES:
            continue
        if norm(phrase) in blob:
            continue
        violations.append(Violation("employer", f"name not in source: {phrase}"))
    for match in DEGREE_RE.finditer(text):
        window_start = max(0, match.start() - 40)
        window_end = min(len(text), match.end() + 40)
        window = norm(text[window_start:window_end])
        if window and window not in norm(record.raw_text) and not any(
            token and token in norm(record.raw_text) for token in window.split() if len(token) > 5
        ):
            violations.append(Violation("qualification", f"qualification not in source: {match.group(0)}"))
    for start, end in RANGE_RE.findall(text):
        if not any(dates_compatible(start, end, role.start, role.end) for role in record.roles) and not any(
            dates_compatible(start, end, edu.start, edu.end) for edu in record.education
        ):
            violations.append(Violation("date", f"date range not in source: {start} - {end}"))
    return violations


def validate_interview(record: SourceRecord, job: JobPost, interview: InterviewContent) -> list[Violation]:
    source_norm = norm(record.raw_text)
    violations: list[Violation] = []
    for line in interview.evidence:
        if norm(line) not in source_norm:
            violations.append(Violation("figure", "interview evidence is not a source bullet"))
    prose = "\n".join([interview.intro, *interview.questions, *interview.ask_them])
    violations.extend(validate_prose(record, job, prose, extra_allowed=interview.intro))
    return violations


def violation_codes(violations: list[Violation]) -> str:
    codes = []
    for item in violations:
        if item.code not in codes:
            codes.append(item.code)
    return ",".join(codes)
