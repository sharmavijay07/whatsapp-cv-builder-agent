"""End-to-end order: confirm, pay, score, deliver, and keep CV text out of the database."""

from safu.extract import extract_docx, extract_pdf
from safu.samples import CV_TEXT, JOB_TEXT, text_to_pdf
from tests.conftest import post_text


def test_pages_render(client):
    home = client.get("/")
    demo = client.get("/demo")
    assert home.status_code == 200
    assert "job-matched CV" in home.text
    assert "Groq" in home.text
    assert demo.status_code == 200
    assert "Play sample order" in demo.text


def test_health_names_groq_as_the_default_model(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["mode"] == "mock"
    assert body["llm_provider"] == "groq"
    assert body["llm_model"] == "openai/gpt-oss-120b"
    assert body["llm_live"] is False
    assert body["llm_fallback"] == "safe_local"
    assert body["payment"] == "stub"


def test_order_is_not_generated_until_confirm_and_pay(client):
    created = client.post("/api/demo/sessions")
    session_id = created.json()["session_id"]
    assert "full name" in created.json()["messages"][-1]["text"].lower()

    named = post_text(client, session_id, "Amina Wanjiku")
    assert "CV" in named.json()["messages"][-1]["text"]

    uploaded = client.post(
        f"/api/demo/sessions/{session_id}/messages",
        data={"message_id": "cv-upload"},
        files={"file": ("amina-cv.pdf", text_to_pdf(CV_TEXT), "application/pdf")},
    )
    assert uploaded.status_code == 200
    assert "job post" in uploaded.json()["messages"][-1]["text"].lower()

    job = post_text(client, session_id, JOB_TEXT)
    confirmation = job.json()["messages"][-1]["text"]
    assert "SwiftHaul Logistics" in confirmation
    assert "Reply YES" in confirmation
    assert job.json()["inspector"]["database_contains_cv_text"] is False
    assert job.json()["inspector"]["database_row"]["pack_id"] is None
    runtime = client.app.state.runtime
    assert runtime.vault.items == {}

    early_pay = post_text(client, session_id, "PAY")
    assert early_pay.json()["inspector"]["database_row"]["paid"] is False
    assert runtime.vault.items == {}

    agreed = post_text(client, session_id, "YES")
    assert "PAY" in agreed.json()["messages"][-1]["text"]
    assert agreed.json()["inspector"]["database_row"]["paid"] is False
    assert runtime.vault.items == {}

    delivered = post_text(client, session_id, "PAY", message_id="pay-once")
    messages = delivered.json()["messages"]
    blob = "\n".join(item["text"] for item in messages)
    assert "ATS score:" in blob
    assert "Job match:" in blob
    filenames = [item.get("filename") for item in messages]
    for name in ("CV.docx", "CV.pdf", "Cover-Letter.docx", "Cover-Letter.pdf", "Interview-Prep.docx", "Interview-Prep.pdf"):
        assert name in filenames

    duplicate = post_text(client, session_id, "PAY", message_id="pay-once")
    assert [item.get("filename") for item in duplicate.json()["messages"]].count("CV.docx") == 1

    docx_token = next(item["token"] for item in messages if item.get("filename") == "CV.docx")
    pdf_token = next(item["token"] for item in messages if item.get("filename") == "CV.pdf")
    docx_text = extract_docx(client.get(f"/files/{docx_token}").content)
    pdf_text = extract_pdf(client.get(f"/files/{pdf_token}").content)
    for text in (docx_text, pdf_text):
        assert "Amina Wanjiku" in text
        assert "SwiftHaul Logistics" in text
        assert "Diploma in Supply Chain Management" in text
        assert "Unilever" not in text
        assert "300%" not in text
        assert "ATS score" not in text

    cover = extract_docx(
        client.get(
            "/files/" + next(item["token"] for item in messages if item.get("filename") == "Cover-Letter.docx")
        ).content
    )
    assert "Freightline Kenya" in cover
    assert "Harvard" not in cover

    inspector = delivered.json()["inspector"]
    assert inspector["database_row"]["status"] == "delivered"
    assert inspector["database_row"]["paid"] is True
    assert inspector["database_contains_cv_text"] is False
    assert inspector["ephemeral"]["cv_in_memory"] is False
    assert inspector["llm"]["provider"] == "groq"
    assert inspector["llm"]["model"] == "openai/gpt-oss-120b"
    assert "cycle counts" in blob

    order_id = inspector["database_row"]["id"]
    assert not (runtime.work_dir / f"{order_id}.json").exists()
    stored = runtime.store.dump()
    assert "SwiftHaul" not in str(stored)
    assert "Cut dispatch errors" not in str(stored)
