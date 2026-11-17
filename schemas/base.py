"""Base model shared by every schema."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Rejects unknown fields, strips whitespace and re-validates on assignment.

    Unknown fields are rejected so LLM output that invents keys fails loudly instead of
    being silently dropped.
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )
