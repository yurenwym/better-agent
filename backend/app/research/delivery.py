"""Versioned section delivery shared by production and paired evaluation."""
import re

TRANSFORM_VERSION = "research-section-v1"


def deliver_section(raw: str, heading: str, finish_reason: str | None = None) -> dict:
    body = re.sub(r"^\s*#{1,6}\s+[^\n]+\n+", "", raw.strip(), count=1)
    if not body.strip() or finish_reason == "length":
        raise ValueError("research section is empty or truncated")
    normalized = re.sub(r"\[\[(source_[^\]\s]+)\]\]", r"[[source:\1]]", body)
    return {"raw": raw, "normalized": normalized, "delivered": f"## {heading}\n\n{normalized}", "transform_version": TRANSFORM_VERSION}
