// Reviewers: one approval's case (GET /approvals/{id}). Left: the complaint, the order's facts,
// the checks passed and the policy text cited. Right: a sticky decision panel with the stored
// amount, a note, approve or reject (each confirmed in a dialog that states the amount), and
// then the refund's status.
//
// The page computes nothing: the amount is the stored proposal's, the facts and eligibility
// come from the backend's rules, and the policy text is our own stored section text.
//
// Below the evidence, the AI analysis (ADR-021): what the agents did, and the AI's own
// recommendation, labelled as AI-generated and unverified. Verified cards carry a "Verified
// from records" label; AI content uses its own colour, so the two never look alike.

import { BadgeCheck, Check, Clock, ExternalLink, FileText, Quote, ShieldCheck, X } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { describeError } from "../api/client";
import type { ApprovalCase, CaseFacts, DecisionResponse } from "../api/types";
import { useAuth } from "../auth/auth";
import { AiAnalysisSection } from "../components/ai/AiAnalysis";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PageHeader } from "../components/PageHeader";
import { DetailSkeleton, ErrorState } from "../components/states";
import { StatusBadge } from "../components/StatusBadge";
import { Alert } from "../components/ui/alert";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { FieldHint, Label, Textarea } from "../components/ui/form";
import { useToast } from "../components/ui/toast-context";
import { useApiGet } from "../hooks/useApiGet";
import { useNow } from "../hooks/useNow";
import { formatDateTime, humanize } from "../lib/labels";
import { formatMoney } from "../lib/money";
import { timeUntil } from "../lib/time";
import { cn } from "../lib/utils";

const MAX_NOTE_LENGTH = 1000; // the API's limit (schemas/approvals.py)
const SETTLED_REFUNDS = new Set(["succeeded", "failed"]);

type Decision = "approve" | "reject";

// Marks a card whose content comes from our own records or rules, never from a model.
function VerifiedLabel() {
  return (
    <Badge tone="primary" title="Read from our records or computed by our rules">
      <ShieldCheck aria-hidden="true" />
      Verified from records
    </Badge>
  );
}

// After an approval the worker executes the refund, and reconciliation may settle it later:
// keep refreshing until the refund has a final status.
function awaitingRefundOutcome(data: ApprovalCase): boolean {
  return (
    data.approval_status === "approved" &&
    (data.refund === null || !SETTLED_REFUNDS.has(data.refund.status))
  );
}

export function ApprovalCasePage({ refreshMs = 3000 }: { refreshMs?: number }) {
  const { approvalId = "" } = useParams();
  const { api } = useAuth();
  const toast = useToast();
  const { data, error, loading, reload } = useApiGet<ApprovalCase>(`/approvals/${approvalId}`);
  const [note, setNote] = useState("");
  const [confirming, setConfirming] = useState<Decision | null>(null);
  const [result, setResult] = useState<DecisionResponse | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const waiting = data !== null && awaitingRefundOutcome(data);
  useEffect(() => {
    if (!waiting) return;
    const timer = setInterval(reload, refreshMs);
    return () => {
      clearInterval(timer);
    };
  }, [waiting, reload, refreshMs]);

  async function decide(decision: Decision) {
    setBusy(true);
    setActionError(null);
    try {
      const body = note.trim() === "" ? { decision } : { decision, note: note.trim() };
      const response = await api.request<DecisionResponse>(`/approvals/${approvalId}/decision`, {
        method: "POST",
        body,
      });
      setResult(response);
      toast.success(decision === "approve" ? "Refund approved" : "Refund rejected", response.message);
      reload();
    } catch (caught) {
      setActionError(describeError(caught));
      reload(); // e.g. already decided or expired: show the current state
    } finally {
      setBusy(false);
      setConfirming(null);
    }
  }

  if (error !== null) return <ErrorState message={error} onRetry={reload} />;
  if (loading || data === null) {
    return (
      <>
        <PageHeader title="Refund case" description="Loading the evidence…" />
        <DetailSkeleton />
      </>
    );
  }

  const amount = formatMoney(data.proposal.amount_minor, data.proposal.currency);

  return (
    <>
      <PageHeader
        title="Refund case"
        description={
          <>
            {data.customer_name} · {data.proposal.order_number} · raised {formatDateTime(data.created_at)}
          </>
        }
        actions={
          <Button asChild variant="outline" size="sm">
            <Link to={`/runs/${data.run_id}`}>
              <ExternalLink aria-hidden="true" />
              Run timeline
            </Link>
          </Button>
        }
      />

      <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
        {/* Evidence */}
        <div className="space-y-6">
          <Card>
            <CardHeader>
              <div>
                <CardTitle>Customer&apos;s complaint</CardTitle>
                <CardDescription>In the customer&apos;s own words. Untrusted text, shown as written.</CardDescription>
              </div>
            </CardHeader>
            <CardContent>
              <blockquote className="flex gap-3 rounded-lg bg-gray-50 px-4 py-3.5">
                <Quote className="mt-0.5 size-4 shrink-0 text-gray-400" aria-hidden="true" />
                <p className="text-sm leading-relaxed whitespace-pre-line text-gray-800">{data.complaint}</p>
              </blockquote>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <div>
                <CardTitle>Proposal</CardTitle>
                <CardDescription>What the system proposed, as stored and checked by code.</CardDescription>
              </div>
              <VerifiedLabel />
            </CardHeader>
            <CardContent className="space-y-4">
              <p className="text-sm text-gray-800">{data.proposal.reason}</p>
              <div>
                <h3 className="mb-2 text-xs font-medium tracking-wide text-gray-500 uppercase">Checks passed</h3>
                {data.proposal.checks_passed.length === 0 ? (
                  <p className="text-sm text-gray-500">None recorded.</p>
                ) : (
                  <ul className="flex flex-wrap gap-2">
                    {data.proposal.checks_passed.map((check) => (
                      <li key={check}>
                        <Badge tone="success">
                          <Check aria-hidden="true" />
                          {humanize(check)}
                        </Badge>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
              <p className="text-xs text-gray-500">
                Proposal fingerprint <code className="font-mono break-all text-gray-600">{data.proposal.payload_hash}</code>
              </p>
            </CardContent>
          </Card>

          {data.facts !== null && <FactsCard facts={data.facts} currency={data.proposal.currency} />}

          <Card>
            <CardHeader>
              <div>
                <CardTitle>Policy</CardTitle>
                <CardDescription>
                  {data.governing_policy === null ? (
                    "No policy version was found for this order's date."
                  ) : (
                    <>
                      In force for this order: <span className="font-medium text-gray-800">{data.governing_policy.title}</span>{" "}
                      {data.governing_policy.version}
                      {data.governing_policy.refund_window_days !== null &&
                        ` · ${data.governing_policy.refund_window_days}-day return window`}
                    </>
                  )}
                </CardDescription>
              </div>
              <VerifiedLabel />
            </CardHeader>
            <CardContent>
              {data.citations.length === 0 ? (
                <p className="text-sm text-gray-500">The run cited no policy sections.</p>
              ) : (
                <ul className="space-y-3">
                  {data.citations.map((citation) => (
                    <li key={citation.citation} className="rounded-lg border border-gray-200">
                      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-gray-100 bg-gray-50/70 px-4 py-2.5">
                        <p className="flex items-center gap-2 text-sm font-medium text-gray-900">
                          <FileText className="size-4 text-gray-400" aria-hidden="true" />
                          {citation.section_title}
                        </p>
                        <span className="text-xs text-gray-500">
                          {citation.policy_title} {citation.version} ·{" "}
                          <code className="font-mono">{citation.citation}</code>
                        </span>
                      </div>
                      <p className="px-4 py-3 text-sm leading-relaxed whitespace-pre-line text-gray-700">{citation.text}</p>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
        </div>

        {/* Decision. On phones it comes right after the verified evidence, before the AI analysis;
            on wide screens it spans both rows so it stays in view (sticky) beside them. */}
        <aside aria-label="Decision" className="lg:sticky lg:top-20 lg:col-start-2 lg:row-span-2 lg:row-start-1">
          <Card>
            <CardContent className="space-y-5">
              <div className="flex items-center justify-between gap-3">
                <h2 className="text-base font-semibold text-gray-900">Decision</h2>
                <StatusBadge status={data.approval_status} />
              </div>

              <div className="rounded-lg bg-gray-50 px-4 py-4">
                <p className="text-xs font-medium tracking-wide text-gray-500 uppercase">Refund amount</p>
                <p className="mt-1 text-3xl font-semibold tracking-tight text-gray-900 tabular-nums" data-testid="amount">
                  {amount}
                </p>
                <p className="mt-1.5 flex items-start gap-1.5 text-xs text-gray-600">
                  <ShieldCheck className="mt-px size-3.5 shrink-0 text-gray-400" aria-hidden="true" />
                  Computed by the refund rules when the proposal was made.
                </p>
              </div>

              {data.approval_status === "pending" ? (
                <PendingDecision
                  expiresAt={data.expires_at}
                  note={note}
                  onNote={setNote}
                  busy={busy}
                  onChoose={(decision) => {
                    setActionError(null);
                    setConfirming(decision);
                  }}
                />
              ) : (
                <Outcome data={data} result={result} waiting={waiting} />
              )}

              {actionError !== null && <Alert tone="danger">{actionError}</Alert>}
            </CardContent>
          </Card>
        </aside>

        <div className="lg:col-start-1">
          <AiAnalysisSection runId={data.run_id} />
        </div>
      </div>

      <ConfirmDialog
        open={confirming === "approve"}
        title="Approve this refund?"
        description={
          <>
            <span className="font-semibold text-gray-900">{amount}</span> will be refunded to {data.customer_name} for{" "}
            {data.proposal.order_number}. Confirming records your approval; the system then sends the refund to
            the payment service. This cannot be undone.
          </>
        }
        confirmLabel={`Approve ${amount}`}
        busy={busy}
        onConfirm={() => void decide("approve")}
        onCancel={() => {
          setConfirming(null);
        }}
      />
      <ConfirmDialog
        open={confirming === "reject"}
        title="Reject this refund?"
        description={
          <>
            {data.customer_name} will not receive the proposed <span className="font-semibold text-gray-900">{amount}</span>{" "}
            refund for {data.proposal.order_number}. The decision is recorded in the audit trail.
          </>
        }
        confirmLabel="Reject refund"
        tone="danger"
        busy={busy}
        onConfirm={() => void decide("reject")}
        onCancel={() => {
          setConfirming(null);
        }}
      />
    </>
  );
}

interface PendingDecisionProps {
  expiresAt: string;
  note: string;
  onNote: (note: string) => void;
  busy: boolean;
  onChoose: (decision: Decision) => void;
}

function PendingDecision({ expiresAt, note, onNote, busy, onChoose }: PendingDecisionProps) {
  const now = useNow(30_000);
  const left = timeUntil(expiresAt, now);
  const urgent = left.urgency === "urgent" || left.urgency === "expired";
  return (
    <>
      <p
        className={cn(
          "flex items-center gap-1.5 text-sm",
          urgent ? "font-medium text-danger-700" : left.urgency === "soon" ? "text-warning-800" : "text-gray-600",
        )}
        title={formatDateTime(expiresAt)}
      >
        <Clock className="size-4" aria-hidden="true" />
        {left.text}
      </p>
      <div>
        <Label htmlFor="note">Note (optional)</Label>
        <Textarea
          id="note"
          rows={3}
          maxLength={MAX_NOTE_LENGTH}
          placeholder="Why you decided this way"
          aria-describedby="note-hint"
          value={note}
          onChange={(event) => {
            onNote(event.target.value);
          }}
        />
        <FieldHint id="note-hint">Kept in the audit trail. Not shown to the customer.</FieldHint>
      </div>
      <div className="grid gap-2">
        <Button
          size="lg"
          disabled={busy}
          onClick={() => {
            onChoose("approve");
          }}
        >
          <Check aria-hidden="true" />
          Approve refund
        </Button>
        <Button
          variant="danger-outline"
          size="lg"
          disabled={busy}
          onClick={() => {
            onChoose("reject");
          }}
        >
          <X aria-hidden="true" />
          Reject
        </Button>
      </div>
    </>
  );
}

function Outcome({ data, result, waiting }: { data: ApprovalCase; result: DecisionResponse | null; waiting: boolean }) {
  return (
    <div className="space-y-4">
      {result !== null && <Alert tone="success">{result.message}</Alert>}
      <dl className="space-y-3 text-sm">
        <div className="flex items-center justify-between gap-3">
          <dt className="text-gray-500">Decided</dt>
          <dd className="text-gray-800">{formatDateTime(data.decided_at)}</dd>
        </div>
        <div className="flex items-start justify-between gap-3">
          <dt className="text-gray-500">Refund</dt>
          <dd className="text-right" data-testid="refund-status">
            {data.refund === null ? (
              <span className="text-gray-700">{data.approval_status === "approved" ? "Being processed…" : "None"}</span>
            ) : (
              <span className="flex flex-col items-end gap-1">
                <StatusBadge status={data.refund.status} />
                <span className="font-medium text-gray-900 tabular-nums">
                  {formatMoney(data.refund.amount_minor, data.proposal.currency)}
                </span>
                {data.refund.provider_reference !== null && (
                  <span className="font-mono text-xs break-all text-gray-500">{data.refund.provider_reference}</span>
                )}
              </span>
            )}
          </dd>
        </div>
      </dl>
      {waiting && (
        <p className="flex items-center gap-2 text-xs text-gray-500" aria-live="polite">
          <span className="size-1.5 animate-pulse rounded-full bg-primary-500" aria-hidden="true" />
          Checking for updates…
        </p>
      )}
    </div>
  );
}

function FactsCard({ facts, currency }: { facts: CaseFacts; currency: string }) {
  const rows: [string, string][] = [
    ["Order status", humanize(facts.order_status)],
    ["Delivered", formatDateTime(facts.delivered_at)],
    ["Return", facts.return_status === null ? "None" : humanize(facts.return_status)],
    ["Return requested", formatDateTime(facts.return_requested_at)],
    ["Return received", formatDateTime(facts.return_received_at)],
    ["Inspection", facts.inspection_status === null ? "—" : humanize(facts.inspection_status)],
    ["Paid", formatMoney(facts.paid_amount_minor, currency)],
    ["Refunded", formatMoney(facts.succeeded_refunds_minor, currency)],
    ["Refunds in progress", formatMoney(facts.in_progress_refunds_minor, currency)],
    ["Refundable balance", formatMoney(facts.refundable_balance_minor, currency)],
    ["Return window", facts.refund_window_days === null ? "No policy" : `${facts.refund_window_days} days`],
  ];
  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>Order facts</CardTitle>
          <CardDescription>Read from the order records just now, with the rules applied now.</CardDescription>
        </div>
        <div className="flex flex-wrap gap-2">
          <VerifiedLabel />
          {facts.eligible_now ? (
            <Badge tone="success">
              <BadgeCheck aria-hidden="true" />
              Eligible now
            </Badge>
          ) : (
            <Badge tone="danger">
              <X aria-hidden="true" />
              Not eligible now
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
          {rows.map(([label, value]) => (
            <div key={label} className="flex justify-between gap-4 border-b border-gray-100 pb-2 sm:block sm:border-0 sm:pb-0">
              <dt className="text-xs text-gray-500">{label}</dt>
              <dd className="text-sm font-medium text-gray-900 tabular-nums sm:mt-0.5">{value}</dd>
            </div>
          ))}
        </dl>
        {!facts.eligible_now && facts.reasons_now.length > 0 && (
          <div>
            <h3 className="mb-2 text-xs font-medium tracking-wide text-gray-500 uppercase">Why not, now</h3>
            <ul className="flex flex-wrap gap-2">
              {facts.reasons_now.map((reason) => (
                <li key={reason}>
                  <Badge tone="danger">{humanize(reason)}</Badge>
                </li>
              ))}
            </ul>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
