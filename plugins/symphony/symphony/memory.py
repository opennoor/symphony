"""Redaction for text Symphony is about to persist."""

import re


_SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token)\b\s*[:=]\s*\S+"
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(
        r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z]+ )?PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
               r"|glpat-[A-Za-z0-9_-]{20,}|sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}"
               r"|xox[baprs]-[A-Za-z0-9-]{10,})\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)


def redact_secrets(text: str) -> str:
    """Redact credential-shaped text before durable storage.

    Structured storage also masks credential-keyed fields, including provider
    naming variants. These text patterns are a second line of defence; no
    pattern set can recognise every credential pasted into arbitrary text.
    """
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text
