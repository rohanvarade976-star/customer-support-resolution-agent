// Fixed wording for the readiness verdict and the escalation reasons (ADR-022). The API sends
// codes computed by code; these turn them into short sentences and say which section of the
// case page holds the evidence, so that section can open by itself. Nothing here decides
// anything: an unknown code is shown as itself, never hidden.

import type { ReadinessReason } from "../api/types";
import { riskText } from "./analysis";
import { humanize } from "./labels";

// The case page's collapsible sections, plus the decision panel.
export type CaseSection = "decision" | "ai-analysis" | "agent-activity" | "tools-used" | "citations" | "order-facts";

interface ReasonText {
  text: string;
  section: CaseSection;
}

function list(codes: string[], format: (code: string) => string = humanize): string {
  return codes.map(format).join(", ");
}

const READINESS: Record<string, (codes: string[]) => ReasonText> = {
  APPROVAL_NOT_PENDING: (codes) => ({ text: `Already decided${codes.length > 0 ? ` (${list(codes)})` : ""}`, section: "decision" }),
  APPROVAL_EXPIRED: () => ({ text: "The approval has expired", section: "decision" }),
  ORDER_UNAVAILABLE: () => ({ text: "The order's records could not be read now", section: "order-facts" }),
  NOT_ELIGIBLE_NOW: (codes) => ({
    text: `No longer eligible${codes.length > 0 ? `: ${list(codes)}` : ""}`,
    section: "order-facts",
  }),
  AMOUNT_CHANGED: () => ({ text: "The refundable amount has changed since the proposal", section: "order-facts" }),
  RISK_FLAG: (codes) => ({ text: `Risk flag: ${list(codes, (code) => riskText(code).title)}`, section: "ai-analysis" }),
  AI_DID_NOT_RECOMMEND_REFUND: () => ({ text: "The AI did not recommend a refund", section: "ai-analysis" }),
  VALIDATOR_NOT_PASSED: (codes) => ({
    text: `The AI's draft did not pass the validator${codes.length > 0 ? `: ${list(codes)}` : ""}`,
    section: "ai-analysis",
  }),
  CRITIC_NOT_APPROVED: (codes) => ({
    text: codes.includes("not_run") ? "The critic did not review the draft" : "The critic asked for changes",
    section: "agent-activity",
  }),
  CITATIONS_DROPPED: (codes) => ({
    text: `${codes[0] ?? "Some"} policy citation(s) could not be verified and were dropped`,
    section: "citations",
  }),
  REVIEWER_PROPOSED: () => ({
    text: "Proposed by a reviewer: no AI checks ran, so check the order facts yourself",
    section: "order-facts",
  }),
  EXPIRING_SOON: () => ({ text: "Expires in less than 10 minutes", section: "decision" }),
};

export function readinessText(reason: ReadinessReason): ReasonText {
  const describe = READINESS[reason.code];
  return describe === undefined ? { text: humanize(reason.code), section: "decision" } : describe(reason.codes);
}

// The sections a list of reasons points at: they open by themselves on the case page.
export function sectionsFor(reasons: ReadinessReason[]): Set<CaseSection> {
  return new Set(reasons.map((reason) => readinessText(reason).section));
}

const ESCALATION: Record<string, (codes: string[]) => string> = {
  AI_CHOSE_TO_ESCALATE: () => "The AI decided a person should handle this",
  DRAFT_FAILED_CHECKS: (codes) => `Every AI draft failed our checks${codes.length > 0 ? `: ${list(codes)}` : ""}`,
  AI_UNAVAILABLE: () => "The AI service was unavailable",
  WORKFLOW_ERROR: (codes) => `A step failed${codes.length > 0 ? `: ${list(codes)}` : ""}`,
  PROPOSAL_REFUSED: (codes) => `The refund rules refused the proposal${codes.length > 0 ? `: ${list(codes)}` : ""}`,
  REFUND_NOT_EXECUTED: (codes) => `The approved refund was not sent${codes.length > 0 ? `: ${list(codes)}` : ""}`,
  APPROVAL_EXPIRED: () => "Nobody decided the refund in time",
  JOB_FAILED: () => "A background job kept failing",
  REPLY_BLOCKED: () => "The output guardrail blocked a reply",
  RISK_FLAG: (codes) => `Risk flag: ${list(codes, (code) => riskText(code).title)}`,
  UNKNOWN: () => "No reason was recorded",
};

export function escalationText(code: string, codes: string[]): string {
  return ESCALATION[code]?.(codes) ?? humanize(code);
}

// Why the output guardrail refused a reviewer's reply (domain/output_guard.py codes).
const REPLY_ISSUES: Record<string, string> = {
  REPLY_INTERNAL_ID: "It contains an internal identifier (an id or a code). Remove it.",
  REPLY_FOREIGN_ORDER: "It names an order this customer does not own.",
  REPLY_CONTACT_DETAILS: "It contains an email address or a phone number.",
};

export function replyIssueText(code: string): string {
  return REPLY_ISSUES[code] ?? humanize(code);
}
