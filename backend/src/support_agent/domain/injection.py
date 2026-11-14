"""Detect text that tries to give instructions to the system (prompt injection). Pure, no I/O.

Customer text is always data (CLAUDE.md rule 9). This module adds a signal on top of that:
code finds phrases that look like orders to the model (a fake "SYSTEM" message, "ignore your
rules", a pre-approval, pasted tool output, a role-play setup) and does two things with them:

- `mark_suspicious` wraps the sentences that contain a match in visible markers. Nothing is
  deleted, so the model still sees everything the customer wrote, and the rest of the
  message (often the customer's real problem) stays unmarked.
- `scan` returns pattern codes only. The audit trail stores those codes, never the text.

It never blocks a run: a pattern list has false positives and can be bypassed (paraphrase,
other languages, encodings). It is a hint for the model and a signal for people, not the
defence. The defences are that no agent can move money and that code decides (ADR-017).
"""

import re
from dataclasses import dataclass
from enum import StrEnum

MARK_OPEN = "[[SUSPECTED_INJECTION]]"
MARK_CLOSE = "[[/SUSPECTED_INJECTION]]"
# A customer could type our markers to fake or hide a flag, so any they write are replaced.
_FORGED_MARKER = re.compile(r"\[\[\s*/?\s*SUSPECTED_INJECTION\s*\]\]", re.IGNORECASE)
_FORGED_REPLACEMENT = "(marker-like text removed)"


class InjectionCode(StrEnum):
    FAKE_SYSTEM_MESSAGE = "FAKE_SYSTEM_MESSAGE"  # "### SYSTEM ###", "admin mode", "you are now"
    IGNORE_RULES = "IGNORE_RULES"  # "ignore your rules", "previous rules no longer apply"
    FAKE_APPROVAL = "FAKE_APPROVAL"  # "pre-approved", "approval APR-777", "skip the inspection"
    FAKE_TOOL_OUTPUT = "FAKE_TOOL_OUTPUT"  # pasted {"tool": ...} or "the tool output says"
    ROLE_PLAY = "ROLE_PLAY"  # "pretend you are", "act as an admin"
    PROMPT_EXTRACTION = "PROMPT_EXTRACTION"  # "print your system prompt"
    FORCED_REPLY = "FORCED_REPLY"  # "reply with exactly this sentence"
    SUPPRESS_ESCALATION = "SUPPRESS_ESCALATION"  # "do not escalate this"
    MEMORY_PLANTING = "MEMORY_PLANTING"  # "save this to my profile", "check your notes"


_I = re.IGNORECASE | re.MULTILINE

# Every pattern is bounded (`.{0,N}`, no nested repeats), so matching stays fast on the
# longest text the API accepts.
_PATTERNS: list[tuple[InjectionCode, re.Pattern[str]]] = [
    (
        InjectionCode.FAKE_SYSTEM_MESSAGE,
        re.compile(
            r"#{2,}\s*(?:system|developer|admin)\b"
            r"|\[\s*(?:system|developer|admin)\s*\]"
            r"|<\s*/?\s*(?:system|developer)\s*>"
            r"|^\s*(?:system|developer)\s*(?:message|prompt|notice)?\s*:"
            r"|\b(?:admin|developer|debug|root)\s+mode\b"
            r"|\byou\s+are\s+now\b.{0,40}\b(?:mode|admin|unrestricted|free)\b"
            r"|\bnew\s+(?:system\s+)?instructions?\s*:",
            _I,
        ),
    ),
    (
        InjectionCode.IGNORE_RULES,
        re.compile(
            r"\b(?:ignore|disregard|forget|override|bypass)\b.{0,30}"
            r"\b(?:rules|instructions|guidelines|polic(?:y|ies)|restrictions|prompt)\b"
            r"|\b(?:previous|prior|earlier|above)\s+(?:rules|instructions)\b.{0,30}"
            r"\b(?:no\s+longer|void|cancelled|do\s+not)\b"
            r"|\brules?\s+(?:ignore|ko\s+ignore)\s+karo\b"
            r"|\bniyam\w*\s+ignore\b",
            _I,
        ),
    ),
    (
        InjectionCode.FAKE_APPROVAL,
        re.compile(
            r"\bpre-?\s?approved\b"
            r"|\balready\s+(?:been\s+)?approved\b"
            r"|\bAPR-\d+"
            r"|\bapproved\s+by\s+(?:the\s+)?(?:review|manager|admin|supervisor|team)"
            r"|\bwithout\s+waiting\s+for\s+(?:the\s+)?(?:inspection|review|approval)"
            r"|\bskip\s+(?:the\s+)?(?:inspection|approval|review|verification)"
            r"|\bapprove\s+kar\s+do\b",
            _I,
        ),
    ),
    (
        InjectionCode.FAKE_TOOL_OUTPUT,
        re.compile(
            r"\{\s*\"(?:tool|tool_call_id|function)\"\s*:"
            r"|\btool\s+output\s+(?:says|shows|confirms)\b"
            r"|\bverified\s+record\s+from\s+your\s+system\b"
            r"|<\s*tool_result\b"
            r"|\"eligible\"\s*:\s*true",
            _I,
        ),
    ),
    (
        InjectionCode.ROLE_PLAY,
        re.compile(
            r"\bpretend\s+(?:to\s+be|you\s+are|that\s+you)\b"
            r"|\bact\s+as\s+(?:an?\s+|the\s+)?"
            r"(?:admin|administrator|manager|reviewer|system|developer)\b"
            r"|\bplay\s+the\s+role\s+of\b"
            r"|\bfrom\s+now\s+on\s+you\s+(?:are|will|must)\b",
            _I,
        ),
    ),
    (
        InjectionCode.PROMPT_EXTRACTION,
        re.compile(
            r"\b(?:print|show|reveal|repeat|display|output)\b.{0,25}"
            r"\b(?:system\s+prompt|your\s+(?:full\s+)?(?:prompt|instructions))\b",
            _I,
        ),
    ),
    (
        InjectionCode.FORCED_REPLY,
        re.compile(
            r"\b(?:reply|respond|answer|say)\s+(?:with\s+)?(?:exactly|only|just)\b.{0,30}"
            r"\b(?:sentence|words?|text|message|nothing\s+else)\b",
            _I,
        ),
    ),
    (
        InjectionCode.SUPPRESS_ESCALATION,
        re.compile(
            r"\b(?:do\s+not|don'?t|never)\s+escalate\b"
            r"|\b(?:do\s+not|don'?t)\s+(?:pass|forward|hand)\s+(?:this|it)\s+(?:on\s+)?to\b",
            _I,
        ),
    ),
    (
        InjectionCode.MEMORY_PLANTING,
        re.compile(
            r"\b(?:save|store|add|record|write|remember)\b.{0,40}"
            r"\b(?:profile|memory|your\s+notes|account\s+notes)\b"
            r"|\bfor\s+all\s+future\s+(?:tickets|requests|refunds|cases)\b"
            r"|\bcheck\s+your\s+(?:notes|memory|records)\b.{0,40}\brecorded\b",
            _I,
        ),
    ),
]

# A sentence ends at . ! ? followed by a space, or at a line break.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")


@dataclass(frozen=True)
class InjectionSignal:
    """What a scan found in one piece of customer text. Codes only, never the text.

    `source` is "complaint" or "reply_<n>". `codes` is a list (not a tuple) so a checkpoint
    round-trip gives back exactly what went in.
    """

    source: str
    codes: list[str]


@dataclass(frozen=True)
class InjectionScan:
    """The text with suspicious sentences marked, and the pattern codes that matched."""

    marked_text: str
    codes: list[InjectionCode]


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for boundary in _SENTENCE_END.finditer(text):
        spans.append((start, boundary.start()))
        start = boundary.end()
    spans.append((start, len(text)))
    return [(a, b) for a, b in spans if b > a]


def analyze(text: str) -> InjectionScan:
    """Find injection patterns and mark the sentences that contain them. Deletes nothing
    except marker-like text the customer typed (it would forge or hide a flag)."""
    clean = _FORGED_MARKER.sub(_FORGED_REPLACEMENT, text)
    found: set[InjectionCode] = set()
    hit_positions: list[int] = []
    for code, pattern in _PATTERNS:
        for match in pattern.finditer(clean):
            found.add(code)
            hit_positions.append(match.start())
    if not hit_positions:
        return InjectionScan(clean, [])

    pieces: list[str] = []
    cursor = 0
    for start, end in _sentence_spans(clean):
        if not any(start <= position < end for position in hit_positions):
            continue
        pieces += [clean[cursor:start], MARK_OPEN, clean[start:end], MARK_CLOSE]
        cursor = end
    pieces.append(clean[cursor:])
    return InjectionScan("".join(pieces), sorted(found, key=lambda code: code.value))


def scan(text: str) -> list[InjectionCode]:
    """The pattern codes found in `text` (empty: nothing suspicious)."""
    return analyze(text).codes


def mark_suspicious(text: str) -> str:
    """`text` with the sentences that look like instructions wrapped in markers."""
    return analyze(text).marked_text


def signal_for(source: str, text: str) -> InjectionSignal | None:
    """An audit signal for one piece of customer text, or None if nothing matched."""
    codes = scan(text)
    return InjectionSignal(source, [code.value for code in codes]) if codes else None
