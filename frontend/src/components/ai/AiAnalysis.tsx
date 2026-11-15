// Reviewers: the AI analysis of one run (GET /runs/{id}/analysis, ADR-021), as collapsible
// sections that start closed (ADR-022).
//
// Two kinds of content, kept visibly apart:
// - the AI recommendation: model-written, unverified. Purple "ai" tokens, a Sparkles icon and
//   an "AI-generated · not verified" label; the rationale is rendered as plain text;
// - everything else (agent activity, tools, quality checks, risk flags, cost): computed by
//   code from the audit trail. Neutral cards labelled "Computed by code".
// Nothing here changes an amount or a decision: the page only displays.

import {
  Bot,
  CircleCheck,
  CircleX,
  Cpu,
  ShieldExclamationPoint,
  Sparkles,
  Wrench,
} from "lucide-react";
import type { ReactNode } from "react";

import type { AgentActivity, QualityChecks, RiskFlag, RunAnalysis, RunCost, ToolUse } from "../../api/types";
import { useApiGet } from "../../hooks/useApiGet";
import {
  actionLabel,
  agentLabel,
  agentSummary,
  formatCount,
  formatDuration,
  riskText,
  toolLabel,
} from "../../lib/analysis";
import { humanize } from "../../lib/labels";
import { Badge } from "../ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { CollapsibleSection } from "../ui/collapsible";
import { Skeleton } from "../ui/skeleton";
import { ErrorState } from "../states";

export type AnalysisSection = "ai-analysis" | "agent-activity" | "tools-used";

interface AiAnalysisSectionsProps {
  runId: string;
  isOpen: (section: AnalysisSection) => boolean;
  onOpenChange: (section: AnalysisSection, open: boolean) => void;
}

// Three collapsible sections from one fetch: the AI's recommendation with the code-computed
// checks, the agents' activity, and the tools they used. Collapsed unless the page opens them
// (ADR-022): a normal case needs none of this to be decided.
export function AiAnalysisSections({ runId, isOpen, onOpenChange }: AiAnalysisSectionsProps) {
  const { data, error, loading, reload } = useApiGet<RunAnalysis>(`/runs/${runId}/analysis`);
  if (error !== null) return <ErrorState message={error} onRetry={reload} />;
  if (loading || data === null) {
    return (
      <Card className="space-y-3 p-5" aria-busy="true" aria-label="Loading the AI analysis">
        <Skeleton className="h-5 w-40" />
        <Skeleton className="h-3 w-full" />
      </Card>
    );
  }
  const failedTools = data.tools.filter((tool) => tool.outcome !== "ok").length;
  const flags = data.risk_flags.length;
  return (
    <>
      <CollapsibleSection
        id="ai-analysis"
        title="AI analysis"
        icon={Sparkles}
        summary={
          <>
            Recommends {actionLabel(data.recommendation.action).toLowerCase()} ·{" "}
            {flags === 0 ? "no risk flags" : `${String(flags)} risk flag${flags === 1 ? "" : "s"}`} · critic{" "}
            {criticWord(data.quality.critic_verdict)}
          </>
        }
        open={isOpen("ai-analysis")}
        onOpenChange={(open) => {
          onOpenChange("ai-analysis", open);
        }}
      >
        <div className="space-y-4">
          <AiRecommendationCard analysis={data} />
          <RiskFlagsCard flags={data.risk_flags} />
          <div className="grid gap-4 md:grid-cols-2">
            <QualityChecksCard quality={data.quality} />
            <CostCard cost={data.cost} />
          </div>
        </div>
      </CollapsibleSection>
      <CollapsibleSection
        id="agent-activity"
        title="Agent activity"
        icon={Bot}
        summary={`${String(data.agents.length)} agent steps · ${formatDuration(data.cost.active_ms)} active`}
        badge={<ComputedLabel />}
        open={isOpen("agent-activity")}
        onOpenChange={(open) => {
          onOpenChange("agent-activity", open);
        }}
      >
        <AgentActivityList agents={data.agents} />
      </CollapsibleSection>
      <CollapsibleSection
        id="tools-used"
        title="Tools used"
        icon={Wrench}
        summary={`${String(data.tools.length)} lookups · ${failedTools === 0 ? "all succeeded" : `${String(failedTools)} failed`}`}
        badge={<ComputedLabel />}
        open={isOpen("tools-used")}
        onOpenChange={(open) => {
          onOpenChange("tools-used", open);
        }}
      >
        <ToolsList tools={data.tools} />
      </CollapsibleSection>
    </>
  );
}

function criticWord(verdict: string | null): string {
  return verdict === null ? "did not run" : verdict === "approve" ? "approved" : "asked for changes";
}

// The one piece of model text. Plain text only: React escapes it, nothing renders HTML.
export function AiRecommendationCard({ analysis }: { analysis: RunAnalysis }) {
  const rec = analysis.recommendation;
  return (
    <section
      aria-labelledby="ai-recommendation-heading"
      className="overflow-hidden rounded-xl border border-ai-200 bg-white shadow-sm"
      data-testid="ai-recommendation"
    >
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-ai-100 bg-ai-50 px-5 py-3">
        <h3 id="ai-recommendation-heading" className="flex items-center gap-2 text-sm font-semibold text-ai-900">
          <Sparkles className="size-4 text-ai-600" aria-hidden="true" />
          AI recommendation
        </h3>
        <Badge tone="ai" title="Written by the resolution agent. Not checked against records.">
          <Sparkles aria-hidden="true" />
          AI-generated · not verified
        </Badge>
      </div>
      <div className="space-y-3 px-5 py-4">
        <p className="text-sm text-gray-600">
          Recommended action: <span className="font-semibold text-gray-900">{actionLabel(rec.action)}</span>
        </p>
        {rec.rationale !== null ? (
          <p className="text-sm leading-relaxed whitespace-pre-line text-gray-800" data-testid="ai-rationale">
            {rec.rationale}
          </p>
        ) : rec.accepted ? (
          <p className="text-sm text-gray-500">No rationale was recorded for this run.</p>
        ) : (
          <div className="space-y-2">
            <p className="text-sm text-gray-700">
              No AI draft was accepted. Code rejected it and passed the case to a person.
            </p>
            {rec.reason_codes.length > 0 && (
              <ul className="flex flex-wrap gap-2">
                {rec.reason_codes.map((code) => (
                  <li key={code}>
                    <Badge tone="neutral">{humanize(code)}</Badge>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        <p className="text-xs text-gray-500">
          Context only. The amount and the decision rest on the verified records, never on this text.
        </p>
      </div>
    </section>
  );
}

function ComputedLabel() {
  return (
    <Badge tone="neutral" title="Computed by code from the audit trail">
      <Cpu aria-hidden="true" />
      Computed by code
    </Badge>
  );
}

export function RiskFlagsCard({ flags }: { flags: RiskFlag[] }) {
  return (
    <Card data-testid="risk-flags">
      <CardHeader>
        <div>
          <CardTitle>Risk flags</CardTitle>
          <CardDescription>Fixed rules over the audit trail. A model never raises or clears a flag.</CardDescription>
        </div>
        <ComputedLabel />
      </CardHeader>
      <CardContent>
        {flags.length === 0 ? (
          <p className="flex items-center gap-2 text-sm text-success-700">
            <CircleCheck className="size-4" aria-hidden="true" />
            No risk flags
          </p>
        ) : (
          <ul className="space-y-2">
            {flags.map((flag) => {
              const text = riskText(flag.code);
              return (
                <li key={flag.code} className="flex gap-3 rounded-lg border border-warning-200 bg-warning-50 px-3.5 py-2.5">
                  <ShieldExclamationPoint className="mt-0.5 size-4 shrink-0 text-warning-600" aria-hidden="true" />
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-warning-800">
                      {text.title}
                      {flag.count > 1 && <span className="font-normal"> · {flag.count}×</span>}
                    </p>
                    <p className="text-xs text-gray-700">{text.description}</p>
                    {flag.codes.length > 0 && (
                      <p className="mt-1 font-mono text-xs break-all text-gray-600">{flag.codes.join(", ")}</p>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

export function AgentActivityList({ agents }: { agents: AgentActivity[] }) {
  if (agents.length === 0) return <p className="text-sm text-gray-500">No agent has finished yet.</p>;
  return (
    <ol aria-label="Agent activity">
      {agents.map((row, i) => (
        <li key={`${row.agent}-${String(i)}`} className="relative flex gap-3 pb-5 last:pb-0">
          {i < agents.length - 1 && (
            <span aria-hidden="true" className="absolute top-7 -bottom-0.5 left-[13px] w-px bg-gray-200" />
          )}
          <span className="grid size-7 shrink-0 place-items-center rounded-full bg-gray-100 text-gray-600" aria-hidden="true">
            <Bot className="size-3.5" />
          </span>
          <div className="min-w-0 flex-1">
            <p className="flex flex-wrap items-baseline justify-between gap-x-3">
              <span className="text-sm font-medium text-gray-900">{agentLabel(row.agent)}</span>
              <span className="text-xs text-gray-500 tabular-nums">{formatDuration(row.duration_ms)}</span>
            </p>
            <p className="mt-0.5 text-sm text-gray-600">{agentSummary(row)}</p>
          </div>
        </li>
      ))}
    </ol>
  );
}

function Check({ ok, children }: { ok: boolean | null; children: string }) {
  if (ok === null) return <span className="text-gray-500">{children}</span>;
  const Icon = ok ? CircleCheck : CircleX;
  return (
    <span className={ok ? "inline-flex items-center gap-1.5 text-success-700" : "inline-flex items-center gap-1.5 text-danger-700"}>
      <Icon className="size-3.5" aria-hidden="true" />
      {children}
    </span>
  );
}

export function QualityChecksCard({ quality }: { quality: QualityChecks }) {
  const validator =
    quality.validator_passed === null
      ? "Not run"
      : quality.validator_passed
        ? "Passed"
        : `Failed: ${quality.validation_codes.map(humanize).join(", ")}`;
  const critic =
    quality.critic_verdict === null ? "Did not run" : quality.critic_verdict === "approve" ? "Approved" : "Asked for a revision";
  const rows: [string, ReactNode][] = [
    ["Validator (last draft)", <Check ok={quality.validator_passed}>{validator}</Check>],
    ["Revisions", String(quality.revisions)],
    ["Critic", <Check ok={quality.critic_verdict === null ? null : quality.critic_verdict === "approve"}>{critic}</Check>],
    ["Facts grounded / dropped", `${String(quality.facts_grounded)} / ${String(quality.facts_dropped)}`],
    ["Citations verified / dropped", `${String(quality.citations_verified)} / ${String(quality.citations_dropped)}`],
    ["Reply guardrail", <Check ok={!quality.reply_replaced}>{quality.reply_replaced ? "Replaced a reply" : "Passed"}</Check>],
  ];
  return (
    <Card data-testid="quality-checks">
      <CardHeader>
        <CardTitle>Quality checks</CardTitle>
        <ComputedLabel />
      </CardHeader>
      <CardContent>
        <dl className="space-y-2.5 text-sm">
          {rows.map(([label, value]) => (
            <div key={label} className="flex items-start justify-between gap-4">
              <dt className="text-gray-500">{label}</dt>
              <dd className="text-right font-medium text-gray-900 tabular-nums">{value}</dd>
            </div>
          ))}
        </dl>
      </CardContent>
    </Card>
  );
}

export function CostCard({ cost }: { cost: RunCost }) {
  const rows: [string, string][] = [
    ["LLM calls", formatCount(cost.llm_calls)],
    ["Tokens in / out", `${formatCount(cost.input_tokens)} / ${formatCount(cost.output_tokens)}`],
    ["Active time", formatDuration(cost.active_ms)],
    ["Fallback answers", formatCount(cost.fallback_calls)],
  ];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Cost</CardTitle>
        <ComputedLabel />
      </CardHeader>
      <CardContent>
        <dl className="space-y-2.5 text-sm">
          {rows.map(([label, value]) => (
            <div key={label} className="flex items-start justify-between gap-4">
              <dt className="text-gray-500">{label}</dt>
              <dd className="font-medium text-gray-900 tabular-nums">{value}</dd>
            </div>
          ))}
        </dl>
      </CardContent>
    </Card>
  );
}

export function ToolsList({ tools }: { tools: ToolUse[] }) {
  if (tools.length === 0) return <p className="text-sm text-gray-500">No tools were called.</p>;
  return (
    <>
      <p className="mb-3 text-xs text-gray-500">Every record lookup, from the audit trail. Results are not shown here.</p>
      <ul className="divide-y divide-gray-100" aria-label="Tools used">
        {tools.map((tool) => (
          <li key={tool.call_id} className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 py-2.5 first:pt-0 last:pb-0">
            <div className="flex min-w-0 items-center gap-2.5">
              <Wrench className="size-3.5 shrink-0 text-gray-400" aria-hidden="true" />
              <span className="text-sm font-medium text-gray-900">{toolLabel(tool.tool)}</span>
              <span className="text-xs text-gray-500">by {tool.by_code ? "code" : agentLabel(tool.agent)}</span>
            </div>
            <div className="flex items-center gap-2">
              {tool.latency_ms !== null && (
                <span className="text-xs text-gray-500 tabular-nums">{formatDuration(tool.latency_ms)}</span>
              )}
              <Badge tone={tool.outcome === "ok" ? "success" : "danger"}>
                {tool.outcome === "ok" ? "OK" : humanize(tool.outcome)}
              </Badge>
            </div>
          </li>
        ))}
      </ul>
    </>
  );
}
