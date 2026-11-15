// Shared API answers for the AI analysis (ADR-021), used by page tests.

import type { AutomationSummary, RunAnalysis, RunFacts } from "../api/types";

// Model text that looks like markup: it must render as text, never as HTML.
export const RATIONALE = "The return was received on day 12. <b>Approve</b> the full amount.";

export const ANALYSIS: RunAnalysis = {
  run_id: "run-1",
  mode: "multi_agent",
  status: "awaiting_approval",
  recommendation: {
    action: "refund",
    accepted: true,
    reason_codes: [],
    rationale: RATIONALE,
    source: "ai_generated",
    verified: false,
  },
  agents: [
    { agent: "classifier", at: "2026-11-10T12:00:01Z", duration_ms: 420, details: { category: "refund", confidence: 0.95 } },
    { agent: "supervisor", at: "2026-11-10T12:00:02Z", duration_ms: 310, details: { specialists: ["investigator", "policy_agent"], added_by_code: [], extra_round: false } },
    { agent: "investigator", at: "2026-11-10T12:00:05Z", duration_ms: 2400, details: { tool_calls: 4, failed_calls: 0, facts: 3, dropped_facts: 0, calls_by_code: 1 } },
    { agent: "policy_agent", at: "2026-11-10T12:00:05Z", duration_ms: 1900, details: { searches: 2, rules: 3, dropped_rules: 1, conflicting_passages: false, no_relevant_policy: false } },
    { agent: "resolution_agent", at: "2026-11-10T12:00:08Z", duration_ms: 1500, details: { action: "refund", attempts: 2, revisions: 1 } },
    { agent: "critic", at: "2026-11-10T12:00:08Z", duration_ms: 700, details: { verdict: "approve", reviews: 2 } },
  ],
  tools: [
    { call_id: "call_1", tool: "get_order", agent: "investigator", outcome: "ok", latency_ms: 14, by_code: false },
    { call_id: "call_2", tool: "get_return_status", agent: "investigator", outcome: "ok", latency_ms: 11, by_code: false },
    { call_id: "required_get_shipment", tool: "get_shipment", agent: "investigator", outcome: "ok", latency_ms: 38, by_code: true },
    { call_id: "search_1", tool: "search_policy", agent: "policy_agent", outcome: "ok", latency_ms: 95, by_code: false },
  ],
  quality: {
    attempts: 2,
    revisions: 1,
    validator_passed: true,
    validation_codes: [],
    critic_verdict: "approve",
    facts_grounded: 3,
    facts_dropped: 0,
    citations_verified: 3,
    citations_dropped: 1,
    reply_replaced: false,
  },
  risk_flags: [
    { code: "DELIVERY_DATE_CONFLICT", codes: ["EVIDENCE_CONFLICT"], count: 1 },
    { code: "INJECTION_SUSPECTED", codes: ["IGNORE_RULES"], count: 1 },
  ],
  cost: { llm_calls: 9, input_tokens: 14250, output_tokens: 1830, active_ms: 7300, fallback_calls: 0 },
};

export const SUMMARY: AutomationSummary = {
  days: 7,
  since: "2026-11-03T12:00:00Z",
  runs: 42,
  resolved_automatically: 27,
  sent_for_approval: 9,
  escalated: 6,
  median_seconds_to_outcome: 8.4,
};

// Verified order facts as the run status carries them (ADR-023): a delivered order whose return
// was received, and one still in transit with the carrier's expected date.
export const DELIVERED_FACTS: RunFacts = {
  order_number: "ORD-1001",
  order_status: "delivered",
  delivered_at: "2026-11-03T10:00:00Z",
  return_status: "received",
  refund_status: null,
  shipment: null,
};

export const IN_TRANSIT_FACTS: RunFacts = {
  order_number: "ORD-1002",
  order_status: "shipped",
  delivered_at: null,
  return_status: null,
  refund_status: null,
  shipment: { status: "in_transit", expected_delivery: "2026-11-18" },
};
