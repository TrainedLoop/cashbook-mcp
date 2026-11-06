"""Handling of untrusted third-party text (bank descriptions, counterpart names).

Sanitizing (invisible characters, marker characters, length cap), delimiting
(suspicion markers) and heuristic suspicion flags. The flags are an aid, not a
guarantee.
"""

import re
from dataclasses import replace

from cashbook_mcp.models import Transaction

MARK_OPEN = "\u27e6untrusted:suspicious\u27e7"
MARK_CLOSE = "\u27e6/untrusted\u27e7"
MAX_TEXT_CHARS = 500

# Zero-width, bidi controls, soft hyphen, Arabic letter mark, Mongolian vowel
# separator, Unicode tag characters, and C0/C1 controls except \n and \t.
_INVISIBLE = re.compile(
    "["
    "\u200b-\u200f"
    "\u202a-\u202e"
    "\u2060-\u2064"
    "\u2066-\u2069"
    "\ufeff"
    "\u00ad\u061c\u180e"
    "\U000e0000-\U000e007f"
    "\u0000-\u0008\u000b-\u001f\u007f-\u009f"
    "]"
)
_BLANK_RUNS = re.compile(r"\n(?:[ \t]*\n){2,}")
_WHITESPACE = re.compile(r"\s+")


def clean_text(s: str) -> str:
    """Remove invisible/control characters and collapse runs of blank lines."""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _INVISIBLE.sub("", s)
    return _BLANK_RUNS.sub("\n\n", s)


def prepare_text(s: str | None) -> str | None:
    """Clean third-party text: strip marker characters, collapse whitespace, cap length."""
    if s is None:
        return None
    s = clean_text(s).replace("\u27e6", "").replace("\u27e7", "")
    s = _WHITESPACE.sub(" ", s).strip()
    if not s:
        return None
    if len(s) > MAX_TEXT_CHARS:
        s = s[:MAX_TEXT_CHARS] + "\u2026"
    return s


# Names of common AI assistants, so an injection written for any of them is flagged.
_ASSISTANT_NAMES = r"claude|chatgpt|gpt|gemini|copilot|grok|llama|mistral|deepseek"
_OVERRIDE = re.compile(
    r"ignore\s+(?:(?:all|any|the)\s+)?(?:(?:previous|prior|above|earlier)\s+)?instructions"
    r"|disregard"
    rf"|you\s+are\s+(?:now\s+)?(?:an?\s+)?(?:ai|assistant|{_ASSISTANT_NAMES})\b"
    r"|ignor[ae]\s+(?:todas\s+)?(?:as\s+)?instru[c\u00e7][\u00f5o]es"
    r"|voc[e\u00ea]\s+[e\u00e9]\s+(?:um|o)\s+assistente",
    re.IGNORECASE,
)
_ADDRESSES = re.compile(
    rf"\bassistant\s*:|\bai\s+assistant\b|\b(?:{_ASSISTANT_NAMES})\s*,", re.IGNORECASE
)
_ACTION = re.compile(
    r"\bforward\s+(?:this|it|the|all|every|these|emails?|messages?)\b"
    r"|\bforward\b[^\n]{0,60}\bto\s+\S+@"
    r"|\b(?:create|make|write|save)\s+(?:a\s+|an\s+|the\s+)?draft\b"
    r"|\b(?:change|modify|edit|set)\s+(?:your\s+|my\s+|the\s+)?(?:preferences|settings)\b"
    r"|\bupdate\s+(?:my\s+|the\s+)?(?:preferences|settings)\b"
    r"|\bupdate\s+your\s+settings\b"
    r"|\breply\s+to\b[^\n]{0,120}?\bwith\b"
    r"|\b(?:pague|transfira)\s+(?:(?:o|a|os|as|este|esta|esse|essa)\b|r\$|a\s+quantia|\d)"
    r"|\bfa[c\u00e7]a\s+um\s+pix\b",
    re.IGNORECASE,
)


def suspicion_reasons(texts: list[str | None], tool_names: frozenset[str]) -> list[str]:
    """Heuristic reason codes for likely injection attempts, in a fixed order."""
    text = "\n".join(t for t in texts if t)
    lowered = text.lower()
    reasons: list[str] = []
    if _OVERRIDE.search(text):
        reasons.append("override")
    if _ADDRESSES.search(text):
        reasons.append("addresses_assistant")
    if _ACTION.search(text):
        reasons.append("action_request")
    reasons.extend(f"names_tool:{n}" for n in sorted(tool_names) if n.lower() in lowered)
    return reasons


def wrap_suspicious(s: str) -> str:
    return f"{MARK_OPEN}{s}{MARK_CLOSE}"


def has_marker(value: object) -> bool:
    """True if value is a string carrying the suspicion marker (case-insensitive,
    so values passed through SQL upper()/lower() are still detected)."""
    return isinstance(value, str) and MARK_OPEN.casefold() in value.casefold()


def apply_text_policy(t: Transaction, tool_names: frozenset[str]) -> Transaction:
    """Prepare the free-text fields and wrap them in markers when they look suspicious."""
    description = prepare_text(t.description) or ""
    counterpart = prepare_text(t.counterpart_name)
    merchant = prepare_text(t.merchant_name)
    reasons = suspicion_reasons([description, counterpart, merchant], tool_names)
    if reasons:
        description = wrap_suspicious(description) if description else description
        counterpart = wrap_suspicious(counterpart) if counterpart else counterpart
        merchant = wrap_suspicious(merchant) if merchant else merchant
    return replace(
        t,
        description=description,
        counterpart_name=counterpart,
        merchant_name=merchant,
        text_suspicious=bool(reasons),
        suspicion_reasons=tuple(reasons),
    )
