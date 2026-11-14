// Fixed wording for the AI analysis (ADR-021). The API sends codes and counts; these turn
// them into short sentences. Nothing here decides anything: an unknown code is shown as
// itself, so a new backend code is never hidden or guessed at.

import type { AgentActivity, AnalysisDetail } from "../api/types";
import { humanize } from "./labels";

const AGENT_LABELS: Record<string, string> = {
  classifier: "Classifier",
  supervisor: "Supervisor",
  investigator: "Investigator",
  policy_agent: "Policy agent",
  baseline_agent: "Baseline agent",
  resolution_agent: "Resolution agent",
  critic: "Critic",
};

export function agentLabel(agent: string): string {
  return AGENT_LABELS[agent] ?? agent;
}

const ACTION_LABELS: Record<string, string> = {
  refund: "Refund",
  explain_status: "Explain the order status",
  request_info: "Ask the customer for details",
  escalate: "Pass to a person",
};

export function actionLabel(action: string | null): string {
  return action === null ? "No recommendation" : (ACTION_LABELS[action] ?? action);
}

const TOOL_LABELS: Record<string, string> = {
  get_order: "Order record",
  get_return_status: "Return status",
  get_payment_status: "Payment status",
  get_shipment: "Shipment tracking",
  get_refundable_balance: "Refundable balance",
  check_refund_eligibility: "Refund rules",
  search_policy: "Policy search",
};

export function toolLabel(tool: string): string {
  return TOOL_LABELS[tool] ?? tool;
}

interface RiskText {
  title: string;
  description: string;
}

const RISK_TEXT: Record<string, RiskText> = {
  DELIVERY_DATE_CONFLICT: {
    title: "Delivery dates disagree",
    description: "The order record and the carrier report different delivery times.",
  },
  INJECTION_SUSPECTED: {
    title: "Instruction-like text",
    description: "The customer's text contains patterns that try to instruct the system.",
  },
  MISSING_REQUIRED_EVIDENCE: {
    title: "Required record missing",
    description: "A record the checks depend on could not be read.",
  },
  LOW_CLASSIFIER_CONFIDENCE: {
    title: "Low classification confidence",
    description: "The request type was unclear or below the confidence threshold.",
  },
  PROVIDER_FALLBACK: {
    title: "Fallback model used",
    description: "Some answers came from the backup AI provider.",
  },
};

export function riskText(code: string): RiskText {
  return RISK_TEXT[code] ?? { title: humanize(code), description: code };
}

// The customer's "What we checked" card: plain words for each fixed source code.
const SOURCE_TEXT: Record<string, string> = {
  order: "Your order details",
  return: "Your return",
  payment: "Your payment",
  shipment: "Delivery tracking from the carrier",
  policy: "Our refund and delivery policies",
};

export function sourceText(code: string): string | null {
  return SOURCE_TEXT[code] ?? null; // unknown codes are not shown to customers
}

export function formatDuration(ms: number | null): string {
  if (ms === null) return "—";
  if (ms < 1000) return `${String(ms)} ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const minutes = Math.floor(seconds / 60);
  return `${String(minutes)} min ${String(Math.round(seconds % 60))} s`;
}

export function formatCount(n: number): string {
  return n.toLocaleString("en-IN");
}

function num(details: Record<string, AnalysisDetail>, key: string): number {
  const value = details[key];
  return typeof value === "number" ? value : 0;
}

function plural(n: number, one: string, many: string): string {
  return `${String(n)} ${n === 1 ? one : many}`;
}

// A short sentence written by code from the agent's counts (never model text).
export function agentSummary(row: AgentActivity): string {
  const d = row.details;
  switch (row.agent) {
    case "classifier": {
      const category = typeof d.category === "string" ? humanize(d.category) : "Unknown";
      const confidence = typeof d.confidence === "number" ? ` · confidence ${d.confidence.toFixed(2)}` : "";
      return `Classified as ${category}${confidence}`;
    }
    case "supervisor": {
      const chosen = Array.isArray(d.specialists) ? d.specialists.map(agentLabel) : [];
      const text = chosen.length === 0 ? "Chose no specialists" : `Sent to ${chosen.join(" and ")}`;
      return d.extra_round === true ? `${text} · asked for one more investigation round` : text;
    }
    case "investigator": {
      const parts = [plural(num(d, "tool_calls"), "tool called", "tools called"), plural(num(d, "facts"), "fact grounded", "facts grounded")];
      if (num(d, "dropped_facts") > 0) parts.push(`${String(num(d, "dropped_facts"))} dropped`);
      if (num(d, "calls_by_code") > 0) parts.push(`${String(num(d, "calls_by_code"))} added by code`);
      return parts.join(", ");
    }
    case "policy_agent": {
      if (d.no_relevant_policy === true) return "No relevant policy found";
      const parts = [plural(num(d, "rules"), "policy section cited", "policy sections cited")];
      if (num(d, "dropped_rules") > 0) parts.push(`${String(num(d, "dropped_rules"))} dropped`);
      if (d.conflicting_passages === true) parts.push("conflicting passages");
      return parts.join(", ");
    }
    case "baseline_agent":
      return plural(num(d, "tool_calls"), "tool called", "tools called");
    case "resolution_agent": {
      const action = typeof d.action === "string" ? actionLabel(d.action) : "No action";
      const revisions = num(d, "revisions");
      const parts = [`Proposed: ${action}`];
      parts.push(revisions === 0 ? "no revisions" : plural(revisions, "revision", "revisions"));
      if (typeof d.failure === "string") parts.push(`stopped: ${humanize(d.failure)}`);
      return parts.join(", ");
    }
    case "critic":
      return d.verdict === "approve" ? "Critic approved" : d.verdict === "revise" ? "Critic asked for a revision" : "Critic reviewed";
    default:
      return "";
  }
}
