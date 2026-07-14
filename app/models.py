"""
Wire schemas for the collector.

Two distinct shapes live here, and keeping them apart is the point:

  IncomingEvent — what the browser sends us. Contains RAW PII.
  CapiEvent     — what we send to Meta. Contains only hashed/allowed fields.

The browser never decides what Meta receives. Every event is re-assembled
server-side, which is what makes the hashing and the dedup guarantees
enforceable rather than hopeful.
"""

from typing import Literal
from pydantic import BaseModel, Field, field_validator

EventName = Literal["ViewContent", "CompleteRegistration", "Purchase"]


class IncomingEvent(BaseModel):
    """Payload accepted at POST /collect. Raw, untrusted, browser-supplied."""

    event_name: EventName
    event_id: str = Field(min_length=8, max_length=64)
    event_source_url: str

    # Raw PII — hashed server-side, never stored in plaintext.
    email: str | None = None
    phone: str | None = None

    # Meta cookies, plaintext by spec.
    fbc: str | None = None
    fbp: str | None = None

    # Purchase only.
    value: float | None = None
    currency: str | None = None

    @field_validator("currency")
    @classmethod
    def currency_upper(cls, v: str | None) -> str | None:
        return v.upper() if v else None

    def requires_value(self) -> bool:
        return self.event_name == "Purchase"


class CapiEvent(BaseModel):
    """Exactly what goes on the wire to Meta. Nothing extra."""

    event_name: str
    event_time: int
    event_id: str
    event_source_url: str
    action_source: str = "website"
    user_data: dict
    custom_data: dict | None = None

    def to_payload(self) -> dict:
        d = self.model_dump(exclude_none=True)
        if not d.get("custom_data"):
            d.pop("custom_data", None)
        return d
