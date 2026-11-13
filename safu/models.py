from dataclasses import dataclass, field

ORDER_FIELDS = (
    "id",
    "wa_id",
    "step",
    "status",
    "created_at",
    "updated_at",
    "paid",
    "handoff",
    "followup_sent",
    "delivery_attempts",
    "pack_id",
    "last_error_code",
    "awaiting_field",
)


@dataclass
class Role:
    title: str
    employer: str
    start: str
    end: str
    bullets: list[str] = field(default_factory=list)


@dataclass
class Education:
    qualification: str
    institution: str
    start: str
    end: str


@dataclass
class SourceRecord:
    name: str | None
    email: str | None
    phone: str | None
    location: str | None
    summary: str
    skills: list[str]
    roles: list[Role]
    education: list[Education]
    raw_text: str
    confidence: float


@dataclass
class JobPost:
    title: str | None
    company: str | None
    location: str | None
    raw_text: str
    keywords: list[str]
    requirements: list[str]


@dataclass
class Score:
    value: int
    lines: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


@dataclass
class CVContent:
    name: str
    email: str
    phone: str
    location: str
    summary: str
    skills: list[str]
    roles: list[Role]
    education: list[Education]


@dataclass
class InterviewContent:
    posting: list[str]
    evidence: list[str]
    gaps: list[str]
    questions: list[str]
    ask_them: list[str]
    intro: str


@dataclass
class PackContent:
    cv: CVContent
    cover_letter: str
    interview: InterviewContent
    ats: Score
    match: Score


@dataclass
class Order:
    id: str
    wa_id: str
    step: str
    status: str
    created_at: str
    updated_at: str
    paid: bool = False
    handoff: bool = False
    followup_sent: bool = False
    delivery_attempts: int = 0
    pack_id: str | None = None
    last_error_code: str | None = None
    awaiting_field: str | None = None

    def to_dict(self) -> dict:
        return {key: getattr(self, key) for key in ORDER_FIELDS}


def order_from_row(row: dict) -> Order:
    data = {key: row.get(key) for key in ORDER_FIELDS}
    data["paid"] = bool(data.get("paid"))
    data["handoff"] = bool(data.get("handoff"))
    data["followup_sent"] = bool(data.get("followup_sent"))
    data["delivery_attempts"] = int(data.get("delivery_attempts") or 0)
    return Order(**data)


@dataclass
class Session:
    order: Order
    greeted: bool = False
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    cv_text: str | None = None
    record: SourceRecord | None = None
    job_text: str | None = None
    job: JobPost | None = None
    confirmed: bool = False
    paid: bool = False
    unreadable: int = 0
    validation_failures: int = 0
    transcript: list[dict] = field(default_factory=list)


@dataclass
class Incoming:
    message_id: str
    wa_id: str
    text: str | None = None
    filename: str | None = None
    content_type: str | None = None
    data: bytes | None = None
    media_id: str | None = None
    unsupported: bool = False


@dataclass
class Outbound:
    kind: str
    text: str = ""
    token: str | None = None
    filename: str | None = None
    caption: str = ""


@dataclass
class TurnResult:
    replies: list[Outbound]
    finalize: bool = False
