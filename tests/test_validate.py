from safu.compose import build_safe_pack, safe_cv
from safu.models import CVContent, Education, Order, Role, Session
from safu.parse import parse_cv, parse_job
from safu.samples import CV_TEXT, JOB_TEXT
from safu.validate import validate_cv, validate_prose


def _session():
    record = parse_cv(CV_TEXT)
    job = parse_job(JOB_TEXT)
    order = Order("ord_test", "254700000000", "confirm", "active", "t", "t")
    return Session(
        order=order,
        name=record.name,
        email=record.email,
        phone=record.phone,
        location=record.location,
        cv_text=CV_TEXT,
        job_text=JOB_TEXT,
        record=record,
        job=job,
    )


def test_parser_reads_the_sample_cv():
    record = parse_cv(CV_TEXT)
    assert record.name == "Amina Wanjiku"
    assert record.email == "amina.wanjiku@email.com"
    assert record.roles[0].employer == "SwiftHaul Logistics"
    assert record.roles[1].employer == "Mombasa Road Traders"
    assert record.education[0].qualification == "Diploma in Supply Chain Management"
    assert record.confidence >= 0.9


def test_safe_pack_passes_validation_and_scores():
    session = _session()
    pack = build_safe_pack(session)
    assert validate_cv(session.record, pack.cv) == []
    assert validate_prose(session.record, session.job, pack.cover_letter, extra_allowed=session.name) == []
    assert pack.ats.value >= 90
    assert 50 <= pack.match.value <= 95
    assert "excel" in pack.match.matched
    assert "cycle counts" in pack.match.missing
    assert "ATS score" not in pack.cv.summary


def test_validator_blocks_fabricated_facts():
    session = _session()
    cv = safe_cv(session)
    forged = CVContent(
        name=cv.name,
        email=cv.email,
        phone=cv.phone,
        location=cv.location,
        summary="Raised revenue by 300% at Unilever.",
        skills=["Negotiation"],
        roles=[Role("Director", "Unilever", "January 2010", "Present", ["Managed a budget of 9000000"])],
        education=[Education("MBA", "Harvard University", "2008", "2010")],
    )
    codes = {item.code for item in validate_cv(session.record, forged)}
    assert {"employer", "date", "qualification", "figure", "skill"} <= codes
