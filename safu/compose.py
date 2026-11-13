import json
from dataclasses import asdict

from safu.documents import (
    cover_blocks,
    interview_blocks,
    render_blocks_pdf,
    render_cv_docx,
    render_cv_pdf,
    render_prose_docx,
)
from safu.errors import LLMError
from safu.llm import SYSTEM_PROMPT
from safu.logutil import log_event
from safu.models import CVContent, Education, InterviewContent, PackContent, Role
from safu.scores import ats_score, job_match_score
from safu.validate import (
    _source_numbers,
    validate_cv,
    validate_interview,
    validate_prose,
    violation_codes,
)


def safe_cv(session) -> CVContent:
    record = session.record
    summary = (record.summary or "").strip()
    if not summary:
        summary = " ".join(
            f"{role.title} at {role.employer} ({role.start} - {role.end})." for role in record.roles
        )
    return CVContent(
        name=session.name or record.name or "",
        email=session.email or record.email or "",
        phone=session.phone or record.phone or "",
        location=session.location or record.location or "",
        summary=summary,
        skills=list(record.skills),
        roles=[
            Role(role.title, role.employer, role.start, role.end, list(role.bullets)) for role in record.roles
        ],
        education=[
            Education(item.qualification, item.institution, item.start, item.end) for item in record.education
        ],
    )


def local_interview(session) -> InterviewContent:
    record = session.record
    job = session.job
    keywords = job.keywords or []
    evidence: list[str] = []
    for role in record.roles:
        for bullet in role.bullets:
            if any(keyword in bullet.casefold() for keyword in keywords):
                evidence.append(bullet)
    if not evidence:
        for role in record.roles:
            evidence.extend(role.bullets[:1])
    cv_norm = record.raw_text.casefold()
    gaps = [
        f"{keyword} is on the posting and not on your CV. Prepare an honest answer. Do not invent a metric."
        for keyword in keywords
        if keyword not in cv_norm
    ]
    questions = []
    for role in record.roles[:2]:
        questions.append(f"Walk through your work as {role.title} at {role.employer}.")
    matched = [keyword for keyword in keywords if keyword in cv_norm]
    for keyword in matched[:3]:
        questions.append(f"The role involves {keyword}. Which part of your history should we read for that?")
    for keyword in [keyword for keyword in keywords if keyword not in cv_norm][:2]:
        questions.append(
            f"The posting mentions {keyword}, which is not on your CV. How will you answer that honestly?"
        )
    ask_them = [
        "What does a strong first quarter look like in this role?",
        "Which part of the work is hardest to staff right now?",
        "How does the team handle exceptions during a shift?",
    ]
    bits = [f"I'm {session.name}."]
    if record.roles:
        first = record.roles[0]
        bits.append(f"I work as {first.title} at {first.employer} ({first.start} - {first.end}).")
    if len(record.roles) > 1:
        second = record.roles[1]
        bits.append(f"Before that I was {second.title} at {second.employer} ({second.start} - {second.end}).")
    posting = list(job.requirements[:5])
    if not posting and job.raw_text.strip():
        posting = [line.strip() for line in job.raw_text.splitlines() if line.strip()][:4]
    return InterviewContent(
        posting=posting,
        evidence=evidence[:4],
        gaps=gaps[:6],
        questions=questions[:6],
        ask_them=ask_them,
        intro=" ".join(bits),
    )


def local_cover(session, matched: list[str]) -> str:
    record = session.record
    job = session.job
    role = job.title or "the open role"
    where = f" at {job.company}" if job.company else ""
    history = []
    for item in record.roles:
        history.append(f"I worked as {item.title} at {item.employer} ({item.start} - {item.end}).")
        for bullet in item.bullets[:2]:
            history.append(bullet if bullet.endswith(".") else bullet + ".")
    paragraphs = [
        "Dear Hiring Manager,",
        f"I am applying for the {role} role{where}.",
        " ".join(history),
    ]
    if record.education:
        edu = record.education[0]
        paragraphs.append(
            f"I completed {edu.qualification} at {edu.institution} ({edu.start} - {edu.end})."
        )
    if matched:
        paragraphs.append("The posting overlaps with my history on: " + ", ".join(matched[:6]) + ".")
    paragraphs.append(f"You can reach me at {session.email} or {session.phone}.")
    paragraphs.append(session.name or "")
    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph)


def build_safe_pack(session) -> PackContent:
    cv = safe_cv(session)
    match = job_match_score(cv, session.job)
    return PackContent(
        cv=cv,
        cover_letter=local_cover(session, match.matched),
        interview=local_interview(session),
        ats=ats_score(cv),
        match=match,
    )


def _cv_from_draft(draft: dict, session) -> CVContent:
    roles = []
    for item in draft.get("experience") or []:
        if not isinstance(item, dict):
            continue
        bullets = item.get("bullets") or []
        if isinstance(bullets, str):
            bullets = [bullets]
        roles.append(
            Role(
                title=str(item.get("title") or ""),
                employer=str(item.get("employer") or ""),
                start=str(item.get("start") or ""),
                end=str(item.get("end") or ""),
                bullets=[str(bullet).strip() for bullet in bullets if str(bullet).strip()],
            )
        )
    education = []
    for item in draft.get("education") or []:
        if not isinstance(item, dict):
            continue
        education.append(
            Education(
                qualification=str(item.get("qualification") or ""),
                institution=str(item.get("institution") or ""),
                start=str(item.get("start") or ""),
                end=str(item.get("end") or ""),
            )
        )
    skills = draft.get("skills") or []
    if isinstance(skills, str):
        skills = [part.strip() for part in skills.split(",") if part.strip()]
    base = safe_cv(session)
    return CVContent(
        name=base.name,
        email=base.email,
        phone=base.phone,
        location=base.location,
        summary=str(draft.get("summary") or base.summary),
        skills=[str(skill).strip() for skill in skills if str(skill).strip()],
        roles=roles,
        education=education,
    )


def _interview_from_draft(draft: dict, fallback: InterviewContent) -> InterviewContent:
    raw = draft.get("interview") if isinstance(draft.get("interview"), dict) else {}
    questions = raw.get("questions") if isinstance(raw.get("questions"), list) else fallback.questions
    ask_them = raw.get("ask_them") if isinstance(raw.get("ask_them"), list) else fallback.ask_them
    intro = str(raw.get("intro") or fallback.intro)
    return InterviewContent(
        posting=list(fallback.posting),
        evidence=list(fallback.evidence),
        gaps=list(fallback.gaps),
        questions=[str(item) for item in questions if str(item).strip()],
        ask_them=[str(item) for item in ask_them if str(item).strip()],
        intro=intro,
    )


def source_payload(session) -> str:
    record = session.record
    job = session.job
    payload = {
        "candidate": {
            "name": session.name,
            "email": session.email,
            "phone": session.phone,
            "location": session.location,
        },
        "summary": record.summary,
        "skills": record.skills,
        "experience": [asdict(role) for role in record.roles],
        "education": [asdict(item) for item in record.education],
        "allowed_numbers": sorted(_source_numbers(record)),
        "target": {
            "title": job.title,
            "company": job.company,
            "keywords": job.keywords,
            "requirements": job.requirements[:8],
        },
    }
    return json.dumps(payload, ensure_ascii=True)


def _materialize(draft: dict, session, safe: PackContent) -> tuple[PackContent | None, list]:
    cv = _cv_from_draft(draft, session)
    cv_violations = validate_cv(session.record, cv)
    letter = str(draft.get("cover_letter") or "").strip()
    letter_violations = (
        validate_prose(session.record, session.job, letter, extra_allowed=session.name or "")
        if letter
        else []
    )
    if not letter:
        letter_violations = []
        letter = safe.cover_letter
    interview = _interview_from_draft(draft, safe.interview)
    interview_violations = validate_interview(session.record, session.job, interview)
    if cv_violations or letter_violations:
        return None, cv_violations + letter_violations + interview_violations
    if interview_violations:
        interview = safe.interview
    match = job_match_score(cv, session.job)
    return (
        PackContent(
            cv=cv,
            cover_letter=letter,
            interview=interview,
            ats=ats_score(cv),
            match=match,
        ),
        [],
    )


def compose(rt, session) -> PackContent:
    safe = build_safe_pack(session)
    if rt.llm is None:
        log_event(
            "llm_fallback",
            rt.settings.log_path,
            order_id=session.order.id,
            provider=rt.settings.llm_provider,
            model=rt.settings.active_model,
            reason="missing_api_key_or_local",
        )
        return safe
    prompt = source_payload(session)
    try:
        draft = rt.llm.complete_json(SYSTEM_PROMPT, prompt)
    except LLMError as exc:
        code = "llm_rate_limited" if exc.code == "rate_limited" else "llm_failed"
        level = "critical" if exc.code == "rate_limited" else "warning"
        rt.alerter.fire(level, code, session.order.id, exc.code, rt.clock.now().isoformat())
        return safe
    pack, violations = _materialize(draft, session, safe)
    if not violations and pack is not None:
        return pack
    session.validation_failures += 1
    rt.alerter.fire(
        "warning",
        "validation_blocked",
        session.order.id,
        violation_codes(violations) or "invalid_draft",
        rt.clock.now().isoformat(),
    )
    retry = prompt + "\n\nThe previous draft was rejected:\n"
    retry += "\n".join(item.detail for item in violations[:8])
    retry += "\nFix the draft. Do not add employers, dates, qualifications, or numbers."
    try:
        second = rt.llm.complete_json(SYSTEM_PROMPT, retry)
    except LLMError as exc:
        rt.alerter.fire("warning", "llm_failed", session.order.id, exc.code, rt.clock.now().isoformat())
        return safe
    pack, again = _materialize(second, session, safe)
    if again or pack is None:
        rt.alerter.fire(
            "warning",
            "validation_blocked",
            session.order.id,
            "fallback_safe_local",
            rt.clock.now().isoformat(),
        )
        return safe
    return pack


def render_pack(pack: PackContent) -> list[tuple[str, bytes]]:
    letter = cover_blocks(pack.cover_letter)
    interview = interview_blocks(pack.interview)
    return [
        ("CV.docx", render_cv_docx(pack.cv)),
        ("CV.pdf", render_cv_pdf(pack.cv)),
        ("Cover-Letter.docx", render_prose_docx("Cover letter", letter)),
        ("Cover-Letter.pdf", render_blocks_pdf("Cover letter", letter)),
        ("Interview-Prep.docx", render_prose_docx("Interview prep", interview)),
        ("Interview-Prep.pdf", render_blocks_pdf("Interview prep", interview)),
    ]


def delivery_text(pack: PackContent) -> str:
    ats = "\n".join(f"- {line}" for line in pack.ats.lines)
    match = "\n".join(f"- {line}" for line in pack.match.lines)
    return (
        f"Your pack is ready.\n\n"
        f"ATS score: {pack.ats.value}/100\n"
        f"Job match: {pack.match.value}/100\n\n"
        f"ATS\n{ats}\n\n"
        f"Job match\n{match}\n\n"
        "The Word files are editable. Links expire 30 minutes after delivery. "
        "The files are not stored in the database.\n\n"
        "Reply RESEND if you need the links again."
    )
