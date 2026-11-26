import hashlib
import re

_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{5,}\.eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]*"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/\-]+=*"),
    re.compile(r"(?i)\b(?:password|passwd|token|secret|api[_-]?key)\s*[=:]\s*\S+"),
    re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s/:@]+:[^\s/@]+@"),
]

PREVIEW_LEN = 200


def redact(text: str) -> str:
    for pat in _PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def preview(text: str | None, mode: str = "preview") -> str | None:
    """Redacted preview, truncated after redaction so a cut can't expose a secret."""
    if mode != "preview" or not text:
        return None
    return redact(text)[:PREVIEW_LEN]


def sha256(text: str | None) -> str | None:
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
