"""Errors raised by tools. Agents report these back to the LLM instead of crashing."""

from __future__ import annotations


class ToolError(ValueError):
    """Bad tool input, e.g. an unknown place id or an unsupported mode."""
