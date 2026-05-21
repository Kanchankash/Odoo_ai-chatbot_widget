"""
Safety guardrails: injection filter, PII redaction, per-user rate limiter.
"""
import logging
import re
import threading
import time
from typing import Any

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Injection filter
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"ignore\s+(all\s+)?previous\s+instructions",
        r"reveal\s+(your\s+)?system\s+prompt",
        r"act\s+as\s+dan",
        r"developer\s+mode",
        r"jailbreak",
        r"do\s+anything\s+now",
        r"pretend\s+(you\s+are|to\s+be)\s+(not\s+an?\s+)?(AI|assistant|bot|language model)",
        r"you\s+are\s+now\s+(free|unrestricted|unlocked)",
        r"forget\s+(your\s+)?(previous\s+)?(training|guidelines|restrictions|rules)",
        r"bypass\s+(your\s+)?(safety|filter|guard|restriction)",
        r"<\|?system\|?>",
        r"\[INST\]",
    ]
]


def check_injection(message: str) -> tuple[bool, str]:
    """
    Return (is_safe, reason). is_safe=False means the message was flagged.

    Args:
        message: raw user message

    Returns:
        tuple: (True if safe, reason string)
    """
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(message):
            reason = f"Message matches injection pattern: {pattern.pattern[:60]}"
            _logger.warning("INJECTION ATTEMPT blocked: %s", reason)
            return False, reason
    return True, ""


# ---------------------------------------------------------------------------
# PII redaction
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(r"\b(\+?[\d\s\-().]{7,15})\b")
_CC_RE = re.compile(r"\b(?:\d[ \-]?){13,16}\b")


def redact_pii(text: str) -> str:
    """
    Replace emails, phone-like sequences, and card-like sequences with
    redaction tokens. Applied before sending to external providers.

    Args:
        text: message or prompt content

    Returns:
        str: redacted text
    """
    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = _PHONE_RE.sub("[REDACTED_PHONE]", text)
    text = _CC_RE.sub("[REDACTED_CC]", text)
    return text


def should_redact(env) -> bool:
    """Return True if PII redaction is enabled and provider is groq."""
    get = env["ir.config_parameter"].sudo().get_param
    provider = get("ai_chatbot.provider", "local")
    flag = get("ai_chatbot.redact_pii", "False")
    return provider == "groq" and flag.lower() in ("true", "1", "yes")


# ---------------------------------------------------------------------------
# Rate limiter — in-memory token bucket keyed by user id
# ---------------------------------------------------------------------------

_buckets: dict[int, dict[str, Any]] = {}
_bucket_lock = threading.Lock()


def _get_max_rpm(env) -> int:
    try:
        return int(
            env["ir.config_parameter"]
            .sudo()
            .get_param("ai_chatbot.max_requests_per_minute", "20")
        )
    except (TypeError, ValueError):
        return 20


def check_rate_limit(env, user_id: int) -> tuple[bool, str]:
    """
    Token-bucket rate limiter. Refills max_rpm tokens per 60 s.

    Args:
        env: Odoo environment (for reading max_rpm config)
        user_id: Odoo user ID

    Returns:
        tuple: (True if allowed, reason string)
    """
    max_rpm = _get_max_rpm(env)
    now = time.monotonic()

    with _bucket_lock:
        bucket = _buckets.get(user_id)
        if bucket is None:
            _buckets[user_id] = {
                "tokens": max_rpm - 1,
                "last_refill": now,
            }
            return True, ""

        elapsed = now - bucket["last_refill"]
        refill = elapsed * (max_rpm / 60.0)
        bucket["tokens"] = min(max_rpm, bucket["tokens"] + refill)
        bucket["last_refill"] = now

        if bucket["tokens"] >= 1:
            bucket["tokens"] -= 1
            return True, ""

    return False, f"Rate limit exceeded. Max {max_rpm} requests per minute."
