"""Check a customer-facing reply for things that must never reach the customer. Pure, no I/O.

Rules (PRD "Safety and guardrails", CLAUDE.md rule 8, ADR-017):

- No internal identifiers: UUIDs, tool call ids, policy citation codes, validator codes.
- No order number the customer does not own (checked against the customer's own order
  list, which the caller loads from the database).
- No email addresses or phone numbers.

The checks use patterns, so they catch the usual shapes, not every disguise ("a b c at ...").
They return reason codes only, never the matched text, so a failure is safe to log and to
hand back to the model as a revision hint.
"""

import re
from collections.abc import Collection, Iterable
from enum import StrEnum


class ReplyIssue(StrEnum):
    INTERNAL_ID = "REPLY_INTERNAL_ID"
    FOREIGN_ORDER = "REPLY_FOREIGN_ORDER"
    CONTACT_DETAILS = "REPLY_CONTACT_DETAILS"


_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_CALL_ID = re.compile(r"\bcall[_-][A-Za-z0-9]+\b|\btool[_ ]call[_ ]id\b", re.I)
# "refund_policy_v2" or "refund_policy_v2:section_3"
_CITATION = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)*_v\d+\b(?::[\w-]+)?|\bsection_\d+\b")
# Our own codes: REFUND_NOT_ELIGIBLE, CITATION_NOT_GROUNDED, ...
_CODE_NAME = re.compile(r"\b[A-Z]{2,}(?:_[A-Z0-9]+)+\b")

_ORDER_NUMBER = re.compile(r"\bORD[-\s#]*(\d{1,12})\b", re.I)
_ORDER_WORDED = re.compile(r"\border\s*(?:number|no\.?|#)\s*[:#]?\s*(\d{3,12})\b", re.I)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(
    r"(?<![\w,.])\+\d[\d\s().-]{8,}\d(?![\w,])"  # +91 98765 43210
    r"|(?<![\w,.])(?:\+?91[\s-]?|0)?[6-9]\d{9}(?![\w,])"  # Indian mobile, 10 digits
    r"|(?<![\w,.])\d{3}[\s.-]\d{3}[\s.-]\d{4}(?![\w,])"  # 987-654-3210
    r"|(?<![\w,.])\d{5}[\s-]\d{5}(?![\w,])"  # 98765 43210
)
_MIN_TOKEN_LENGTH = 6  # shorter literals would match ordinary words


def _mentioned_orders(text: str) -> set[str]:
    digits = [m.group(1) for m in _ORDER_NUMBER.finditer(text)]
    digits += [m.group(1) for m in _ORDER_WORDED.finditer(text)]
    return {f"ORD-{d}" for d in digits}


def check_reply(
    text: str,
    owned_orders: Collection[str] | None,
    internal_tokens: Iterable[str] = (),
) -> list[ReplyIssue]:
    """Every reason `text` may not be shown to the customer (empty: it may).

    `owned_orders` are the customer's own order numbers ("ORD-2001"). None skips that
    check (callers with no customer context); the workflow always passes it.
    `internal_tokens` are exact strings known to be internal to this run, such as call ids
    and grounded citations.
    """
    issues: list[ReplyIssue] = []
    has_internal = any(p.search(text) for p in (_UUID, _CALL_ID, _CITATION, _CODE_NAME))
    has_internal = has_internal or any(
        len(token) >= _MIN_TOKEN_LENGTH and token in text for token in internal_tokens
    )
    if has_internal:
        issues.append(ReplyIssue.INTERNAL_ID)
    if owned_orders is not None and not _mentioned_orders(text) <= set(owned_orders):
        issues.append(ReplyIssue.FOREIGN_ORDER)
    if _EMAIL.search(text) or _PHONE.search(text):
        issues.append(ReplyIssue.CONTACT_DETAILS)
    return issues
