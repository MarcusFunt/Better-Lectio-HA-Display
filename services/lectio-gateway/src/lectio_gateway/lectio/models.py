import json
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


class LectioCookie(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    value: SecretStr
    domain: str
    path: str = "/"
    secure: bool = True
    expires: float | None = None

    @field_validator("domain")
    @classmethod
    def validate_lectio_domain(cls, value: str) -> str:
        domain = value.lower().lstrip(".")
        if domain != "lectio.dk" and not domain.endswith(".lectio.dk"):
            raise ValueError("Only Lectio-domain cookies may be stored")
        return value.lower()

    @field_validator("path")
    @classmethod
    def validate_cookie_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("Cookie path must start with '/'")
        return value


class AuthenticatedLectioSession(BaseModel):
    """Restorable, secret-bearing Lectio session imported from a browser login."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    school_id: str = Field(pattern=r"^\d+$")
    student_id: str = Field(pattern=r"^\d+$")
    cookies: tuple[LectioCookie, ...]

    def to_json(self) -> str:
        """Serialize secrets for an explicit persistence operation."""
        data = self.model_dump(exclude={"cookies"})
        data["cookies"] = [
            {
                "name": cookie.name,
                "value": cookie.value.get_secret_value(),
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": cookie.secure,
                "expires": cookie.expires,
            }
            for cookie in self.cookies
        ]
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> "AuthenticatedLectioSession":
        return cls.model_validate_json(value)


class LectioLesson(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    start: AwareDatetime
    end: AwareDatetime
    subject: str | None = None
    teacher: str | None = None
    room: str | None = None
    status: Literal["normal", "cancelled", "changed", "exam", "unknown"] = "unknown"
    source_url: str | None = None
    source_id: str | None = None
    details: str | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> "LectioLesson":
        if self.start >= self.end:
            raise ValueError("Lesson end must be after its start")
        return self


class LectioAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    title: str
    description: str | None = None
    due: AwareDatetime | None = None
    subject: str | None = None
    status: str = "unknown"
    source_url: str | None = None
    source_id: str | None = None


class LectioHomework(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    subject: str | None = None
    description: str
    target_lesson_start: AwareDatetime | None = None
    source_url: str | None = None


class LectioCancellation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    original_lesson: LectioLesson
    start: AwareDatetime
    end: AwareDatetime
    subject: str | None = None
    teacher: str | None = None
    room: str | None = None
    reason: str | None = None
    details: str | None = None
    source_url: str | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> "LectioCancellation":
        if self.start >= self.end:
            raise ValueError("Cancellation end must be after its start")
        return self


class LectioSyncStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["unknown", "valid", "expired", "error"] = "unknown"
    last_successful_sync: AwareDatetime | None = None
    error: str | None = None
