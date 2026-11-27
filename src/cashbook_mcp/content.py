"""Handling of untrusted third-party text (bank descriptions, counterpart names).

Sanitizing (invisible characters, marker characters, length cap), delimiting and
heuristic suspicion flags. Every free-text field of third-party origin is delimited
unconditionally between MARK_UNTRUSTED and MARK_CLOSE; MARK_OPEN (the ":suspicious"
variant) is used instead of MARK_UNTRUSTED only when a heuristic also flags the text.
Delimiting never depends on the heuristic, which is an aid, not a guarantee.
Identifier-like fields (tax ids, account numbers, category and type codes) carry
program logic, so they are sanitized and length-capped but not delimited.
"""

import re
from dataclasses import replace

from cashbook_mcp.models import Transaction

MARK_UNTRUSTED = "\u27e6untrusted\u27e7"
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


MAX_IDENTIFIER_CHARS = 64
_IDENTIFIER_DROP = re.compile(r"\s+")
_WRAPPER = re.compile(
    "|".join(re.escape(m) for m in (MARK_OPEN, MARK_UNTRUSTED, MARK_CLOSE)), re.IGNORECASE
)


def prepare_identifier(s: str | None) -> str | None:
    """Clean an identifier-like third-party value (tax id, account number, category or type
    code): no invisible/marker characters, no whitespace runs, length cap."""
    if s is None:
        return None
    s = clean_text(str(s)).replace("\u27e6", "").replace("\u27e7", "")
    s = _WHITESPACE.sub(" ", s).strip()
    if not s:
        return None
    return s[:MAX_IDENTIFIER_CHARS]


_CODE = re.compile(r"[A-Z0-9_]{1,32}")


def prepare_code(s: str | None) -> str | None:
    """Enum-like code (status, error code, currency): ^[A-Z0-9_]{1,32}$.
    Anything else becomes None."""
    s = prepare_identifier(s)
    return s if s is not None and _CODE.fullmatch(s) else None


UNKNOWN_CURRENCY = "XXX"  # ISO 4217 "no currency": never BRL, so v_flows keeps it out of the BRL totals


def prepare_currency(s: str | None, default: str = "BRL") -> str:
    """Currency code: absent gives `default`; a malformed one gives UNKNOWN_CURRENCY, never `default`,
    so a foreign amount is not passed off as BRL."""
    if s is None or s == "":
        return default
    return prepare_code(s) or UNKNOWN_CURRENCY


def unwrap(s: str | None) -> str | None:
    """Remove delimiting markers added by delimit()/wrap_suspicious()."""
    return None if s is None else _WRAPPER.sub("", s)


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


def delimit(s: str, suspicious: bool = False) -> str:
    """Wrap already-prepared third-party text between markers (always; never conditional)."""
    return wrap_suspicious(s) if suspicious else f"{MARK_UNTRUSTED}{s}{MARK_CLOSE}"


def prepare_delimited(s: str | None, suspicious: bool = False) -> str | None:
    """prepare_text() then delimit(); None when nothing is left."""
    text = prepare_text(unwrap(s))
    return None if text is None else delimit(text, suspicious)


def has_marker(value: object) -> bool:
    """True if value is a string carrying the suspicion marker (case-insensitive,
    so values passed through SQL upper()/lower() are still detected)."""
    return isinstance(value, str) and MARK_OPEN.casefold() in value.casefold()


def apply_text_policy(t: Transaction, tool_names: frozenset[str]) -> Transaction:
    """Prepare every third-party text field of a transaction. Free text (description, counterpart
    and merchant names) is always delimited, as ":suspicious" when the heuristic flags any of it;
    identifier-like fields are sanitized."""
    description = prepare_text(t.description) or ""
    counterpart = prepare_text(t.counterpart_name)
    merchant = prepare_text(t.merchant_name)
    reasons = suspicion_reasons(
        [description, counterpart, merchant, prepare_text(t.pluggy_category)], tool_names
    )
    flagged = bool(reasons)
    return replace(
        t,
        description=delimit(description, flagged) if description else description,
        counterpart_name=delimit(counterpart, flagged) if counterpart else counterpart,
        merchant_name=delimit(merchant, flagged) if merchant else merchant,
        counterpart_tax_id=prepare_identifier(t.counterpart_tax_id),
        counterpart_account_number=prepare_identifier(t.counterpart_account_number),
        pluggy_category=prepare_identifier(t.pluggy_category),
        payment_method=prepare_identifier(t.payment_method),
        text_suspicious=flagged,
        suspicion_reasons=tuple(reasons),
    )
