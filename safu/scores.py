import re

from safu.models import CVContent, JobPost, Score

PHRASES = [
    "google sheets",
    "supply chain",
    "inventory control",
    "cycle counts",
    "on-time delivery",
    "dispatch scheduling",
    "written updates",
    "customer updates",
    "record keeping",
    "excel",
    "dispatch",
    "inventory",
    "warehouse",
    "logistics",
    "transport",
    "stock",
    "procurement",
]

_STOP = {
    "with",
    "from",
    "that",
    "this",
    "your",
    "role",
    "and",
    "the",
    "for",
    "our",
    "you",
}


def _norm(value: str) -> str:
    value = value.casefold().replace("–", "-").replace("—", "-")
    value = re.sub(r"[^a-z0-9%+@.' -]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def extract_keywords(text: str) -> list[str]:
    haystack = _norm(text)
    found = []
    for phrase in PHRASES:
        if phrase in haystack and phrase not in found:
            found.append(phrase)
    return found[:12]


def _words(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9']+", text))


def plain_cv(cv: CVContent) -> str:
    lines = [cv.name, cv.email, cv.phone, cv.location, cv.summary, ", ".join(cv.skills)]
    for role in cv.roles:
        lines.append(f"{role.title} {role.employer} {role.start} {role.end}")
        lines.extend(role.bullets)
    for edu in cv.education:
        lines.append(f"{edu.qualification} {edu.institution} {edu.start} {edu.end}")
    return "\n".join(lines)


def ats_score(cv: CVContent) -> Score:
    points = 0
    lines: list[str] = []
    if cv.name:
        points += 8
    if cv.email:
        points += 8
    if cv.phone:
        points += 8
    if cv.location:
        points += 6
    if cv.name and cv.email and cv.phone and cv.location:
        lines.append("Name, email, phone, and location are plain text.")
    else:
        missing = [
            label
            for label, present in (
                ("name", cv.name),
                ("email", cv.email),
                ("phone", cv.phone),
                ("location", cv.location),
            )
            if not present
        ]
        lines.append("Missing contact: " + ", ".join(missing) + ".")
    summary_words = _words(cv.summary)
    if summary_words >= 20:
        points += 12
        lines.append("Summary is present.")
    elif summary_words >= 8:
        points += 6
        lines.append("Summary is short.")
    if len(cv.skills) >= 4:
        points += 10
        lines.append("Skills are a plain list.")
    roles_ok = bool(cv.roles) and all(role.title and role.employer and role.start and role.end for role in cv.roles)
    if roles_ok:
        points += 14
    if cv.roles and all(role.bullets for role in cv.roles):
        points += 10
    if cv.education and all(item.qualification and item.institution for item in cv.education):
        points += 10
        lines.append("Experience and education use standard headings.")
    points += 8  # generator never uses tables, text boxes, or columns
    total_words = _words(plain_cv(cv))
    if 120 <= total_words <= 900:
        points += 6
    return Score(value=min(points, 100), lines=lines[:4])


def job_match_score(cv: CVContent, job: JobPost) -> Score:
    keywords = job.keywords or extract_keywords(job.raw_text)
    haystack = _norm(plain_cv(cv))
    matched = [keyword for keyword in keywords if keyword in haystack]
    missing = [keyword for keyword in keywords if keyword not in haystack]
    if keywords:
        coverage = 100 * len(matched) / len(keywords)
    else:
        coverage = 40
    title_tokens = {token for token in _norm(job.title or "").split() if len(token) > 3 and token not in _STOP}
    role_tokens: set[str] = set()
    for role in cv.roles:
        role_tokens.update(token for token in _norm(role.title).split() if len(token) > 3 and token not in _STOP)
    if title_tokens:
        title_points = round(15 * (len(title_tokens & role_tokens) / len(title_tokens)))
    else:
        title_points = 8
    value = min(100, round(coverage * 0.85 + title_points))
    lines = []
    if matched:
        lines.append("Matched: " + ", ".join(matched[:8]) + ".")
    if missing:
        lines.append("Missing: " + ", ".join(missing[:6]) + ".")
    if not lines:
        lines.append("The job post did not yield keywords to compare.")
    return Score(value=value, lines=lines, matched=matched, missing=missing)
