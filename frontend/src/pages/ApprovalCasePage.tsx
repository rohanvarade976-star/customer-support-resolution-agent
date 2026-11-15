// Reviewers: one approval's case (GET /approvals/{id}), built for a quick, safe decision
// (ADR-022):
//
// 1. A verdict banner computed by code: green "Ready to approve", or amber "Needs attention"
//    with the reasons, each opening the section that holds the evidence.
// 2. Key facts (amount, order, checks, eligibility now, risk, AI action) and the customer's
//    complaint, next to a sticky decision panel, so a normal case needs no scrolling.
// 3. Everything else in collapsible sections, closed by default: the AI analysis, agent
//    activity, tools used, the cited policy text and the order's facts. A section that a
//    needs-attention reason points to opens by itself.
//
// The page computes nothing: the verdict, amount, facts and eligibility come from the API.
// Approve and reject are confirmed in a dialog that states the stored amount. A reviewer who
// proposed this refund by hand cannot decide it (separation of duties): the panel says so.

import { Check, Clock, ExternalLink, FileText, Package, Quote, ShieldCheck, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { describeError } from "../api/client";
import type { ApprovalCase, DecisionResponse } from "../api/types";
import { useAuth } from "../auth/auth";
import { AiAnalysisSections } from "../components/ai/AiAnalysis";
import { KeyFacts } from "../components/case/KeyFacts";
import { EligibleBadge, OrderFactsBody, VerifiedLabel } from "../components/case/OrderFacts";
import { VerdictBanner } from "../components/case/VerdictBanner";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PageHeader } from "../components/PageHeader";
import { DetailSkeleton, ErrorState } from "../components/states";
import { StatusBadge } from "../components/StatusBadge";
import { Alert } from "../components/ui/alert";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent } from "../components/ui/card";
import { CollapsibleSection } from "../components/ui/collapsible";
import { FieldHint, Label, Textarea } from "../components/ui/form";
import { useToast } from "../components/ui/toast-context";
import { useApiGet } from "../hooks/useApiGet";
import { useNow } from "../hooks/useNow";
import { formatDateTime } from "../lib/labels";
import { formatMoney } from "../lib/money";
import { sectionsFor, type CaseSection } from "../lib/readiness";
import { timeUntil } from "../lib/time";
import { cn } from "../lib/utils";

const MAX_NOTE_LENGTH = 1000; // the API's limit (schemas/approvals.py)
const SETTLED_REFUNDS = new Set(["succeeded", "failed"]);

type Decision = "approve" | "reject";

// After an approval the worker executes the refund, and reconciliation may settle it later:
// keep refreshing until the refund has a final status.
function awaitingRefundOutcome(data: ApprovalCase): boolean {
  return (
    data.approval_status === "approved" &&
    (data.refund === null || !SETTLED_REFUNDS.has(data.refund.status))
  );
}

// Which sections are open. They start from the verdict's reasons (once, when the case first
// loads) and then follow the reviewer's clicks; a later refresh never closes what they opened.
function useOpenSections(data: ApprovalCase | null) {
  const [open, setOpen] = useState<Set<CaseSection>>(new Set());
  const initialised = useRef(false);
  useEffect(() => {
    if (data === null || initialised.current) return;
    initialised.current = true;
    setOpen(sectionsFor(data.readiness.reasons));
  }, [data]);

  function setSection(section: CaseSection, isOpen: boolean) {
    setOpen((current) => {
      const next = new Set(current);
      if (isOpen) next.add(section);
      else next.delete(section);
      return next;
    });
  }

  // A verdict reason was clicked: open its section, then bring it into view.
  function show(section: CaseSection) {
    setSection(section, true);
    setTimeout(() => {
      document.getElementById(section)?.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 0);
  }

  return { isOpen: (section: CaseSection) => open.has(section), setSection, show };
}

export function ApprovalCasePage({ refreshMs = 3000 }: { refreshMs?: number }) {
  const { approvalId = "" } = useParams();
  const { api } = useAuth();
  const toast = useToast();
  const { data, error, loading, reload } = useApiGet<ApprovalCase>(`/approvals/${approvalId}`);
  const sections = useOpenSections(data);
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
  const droppedCitations = data.readiness.reasons.some((reason) => reason.code === "CITATIONS_DROPPED");

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

      <div className="mb-4">
        <VerdictBanner readiness={data.readiness} checksPassed={data.proposal.checks_passed.length} onShow={sections.show} />
      </div>

      <div className="grid grid-cols-[minmax(0,1fr)] items-start gap-4 lg:grid-cols-[minmax(0,1fr)_22rem] lg:grid-rows-[auto_1fr] lg:gap-6">
        <div className="space-y-4">
          <KeyFacts data={data} />
          <Card>
            <CardContent className="p-4 sm:p-5">
              <p className="mb-2 text-xs font-medium text-gray-500">
                Customer&apos;s complaint <span className="font-normal">· their own words, shown as written</span>
              </p>
              <blockquote className="flex gap-3">
                <Quote className="mt-0.5 size-4 shrink-0 text-gray-400" aria-hidden="true" />
                <p className="line-clamp-4 text-sm leading-relaxed whitespace-pre-line text-gray-800">{data.complaint}</p>
              </blockquote>
            </CardContent>
          </Card>
        </div>

        {/* Decision. On phones it comes right after the key facts, before the sections; on wide
            screens it spans both rows so it stays in view (sticky) beside them. */}
        <aside
          id="decision"
          aria-label="Decision"
          className="scroll-mt-20 lg:sticky lg:top-20 lg:col-start-2 lg:row-span-2 lg:row-start-1"
        >
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
                  proposedByYou={data.proposed_by_you}
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

        <div className="space-y-3 lg:col-start-1">
          <h2 className="pt-2 text-xs font-medium tracking-wide text-gray-500 uppercase">Details</h2>
          <AiAnalysisSections
            runId={data.run_id}
            isOpen={sections.isOpen}
            onOpenChange={sections.setSection}
          />
          <CollapsibleSection
            id="citations"
            title="Policy cited"
            icon={FileText}
            summary={
              data.citations.length === 0
                ? "No policy sections cited"
                : `${String(data.citations.length)} section${data.citations.length === 1 ? "" : "s"} · our own stored text`
            }
            badge={<VerifiedLabel />}
            open={sections.isOpen("citations")}
            onOpenChange={(open) => {
              sections.setSection("citations", open);
            }}
          >
            {droppedCitations && (
              <Alert tone="warning" className="mb-4">
                Code dropped policy citations the AI gave that do not exist in our policy records. Only verified
                sections are shown.
              </Alert>
            )}
            {data.citations.length === 0 ? (
              <p className="text-sm text-gray-500">The run cited no policy sections.</p>
            ) : (
              <ul className="space-y-3">
                {data.citations.map((citation) => (
                  <li key={citation.citation} className="rounded-lg border border-gray-200">
                    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-gray-100 bg-gray-50/70 px-4 py-2.5">
                      <p className="text-sm font-medium text-gray-900">{citation.section_title}</p>
                      <span className="text-xs text-gray-500">
                        {citation.policy_title} {citation.version}
                      </span>
                    </div>
                    <p className="px-4 py-3 text-sm leading-relaxed whitespace-pre-line text-gray-700">{citation.text}</p>
                  </li>
                ))}
              </ul>
            )}
          </CollapsibleSection>
          <CollapsibleSection
            id="order-facts"
            title="Order facts"
            icon={Package}
            summary={data.facts === null ? "The order could not be read now" : "Read just now, rules applied now"}
            badge={data.facts === null ? <Badge tone="danger">Unavailable</Badge> : <EligibleBadge facts={data.facts} />}
            open={sections.isOpen("order-facts")}
            onOpenChange={(open) => {
              sections.setSection("order-facts", open);
            }}
          >
            {data.facts === null ? (
              <p className="text-sm text-gray-500">The order's records could not be read. Do not approve until they can.</p>
            ) : (
              <OrderFactsBody facts={data.facts} currency={data.proposal.currency} policy={data.governing_policy} />
            )}
            <div className="mt-4 space-y-1 border-t border-gray-100 pt-4 text-xs text-gray-500">
              <p>
                Proposal: <span className="text-gray-700">{data.proposal.reason}</span>
              </p>
              <p>
                Fingerprint <code className="font-mono break-all text-gray-600">{data.proposal.payload_hash}</code>
              </p>
            </div>
          </CollapsibleSection>
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
  proposedByYou: boolean;
  onChoose: (decision: Decision) => void;
}

function PendingDecision({ expiresAt, note, onNote, busy, proposedByYou, onChoose }: PendingDecisionProps) {
  const now = useNow(30_000);
  const left = timeUntil(expiresAt, now);
  const urgent = left.urgency === "urgent" || left.urgency === "expired";
  const time = (
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
  );
  if (proposedByYou) {
    // Separation of duties: the API refuses this reviewer's decision (SELF_DECISION_FORBIDDEN).
    return (
      <>
        {time}
        <Alert tone="info" title="Another reviewer must decide">
          You proposed this refund, so you cannot approve or reject it. It stays in the queue for a colleague.
        </Alert>
      </>
    );
  }
  return (
    <>
      {time}
      <div>
        <Label htmlFor="note">Note (optional)</Label>
        <Textarea
          id="note"
          rows={2}
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
