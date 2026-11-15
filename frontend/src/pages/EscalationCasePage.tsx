// Reviewers: one escalated ticket (GET /reviewer/escalations/{ticketId}, ADR-022).
//
// Top: why it was escalated, in plain words (codes computed by code). Left: the customer's
// complaint and what they see now; below, the AI analysis and the order's facts in collapsed
// sections. Right: the three things a person can do, each confirmed in a dialog first:
//
// - reply to the customer: the API checks it with the output guardrail and refuses it (saying
//   why) if it holds an internal id, another customer's order or contact details. It is never
//   sent to a model;
// - propose a refund: the existing reviewer endpoint. The refund rules compute the amount and a
//   different reviewer must approve it (separation of duties);
// - resolve with a note: only after a reply, so the customer knows the outcome. The note stays
//   in the audit trail and is never shown to the customer.

import { ExternalLink, Headset, MessageSquareText, Package, Quote, ReceiptText, Send, TriangleAlert } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { ApiError, describeError } from "../api/client";
import type { EscalationActionResponse, EscalationCase, ProposalResponse } from "../api/types";
import { useAuth } from "../auth/auth";
import { AiAnalysisSections, type AnalysisSection } from "../components/ai/AiAnalysis";
import { EligibleBadge, OrderFactsBody } from "../components/case/OrderFacts";
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
import { formatDateTime, ticketStatusLabel } from "../lib/labels";
import { formatMoney } from "../lib/money";
import { escalationText, replyIssueText } from "../lib/readiness";
import { formatRelative } from "../lib/time";

const MAX_REPLY_LENGTH = 2000; // the API's limits (schemas/escalations.py)
const MAX_NOTE_LENGTH = 1000;

type Action = "reply" | "propose" | "resolve";
type Section = AnalysisSection | "order-facts";

// A refused reply's codes (422 REPLY_REJECTED), as plain sentences.
function replyProblems(caught: unknown): string[] | null {
  if (!(caught instanceof ApiError) || caught.code !== "REPLY_REJECTED") return null;
  const details = caught.details;
  const codes =
    typeof details === "object" && details !== null && "codes" in details && Array.isArray(details.codes)
      ? details.codes.filter((code): code is string => typeof code === "string")
      : [];
  return codes.map(replyIssueText);
}

export function EscalationCasePage() {
  const { ticketId = "" } = useParams();
  const { api } = useAuth();
  const toast = useToast();
  const navigate = useNavigate();
  const { data, error, loading, reload } = useApiGet<EscalationCase>(`/reviewer/escalations/${ticketId}`);
  const [reply, setReply] = useState("");
  const [note, setNote] = useState("");
  const [confirming, setConfirming] = useState<Action | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<{ action: Action; message: string; problems: string[] } | null>(null);
  const [open, setOpen] = useState<Set<Section>>(new Set());

  function setSection(section: Section, isOpen: boolean) {
    setOpen((current) => {
      const next = new Set(current);
      if (isOpen) next.add(section);
      else next.delete(section);
      return next;
    });
  }

  async function run(action: Action, perform: () => Promise<void>) {
    setBusy(true);
    setActionError(null);
    try {
      await perform();
    } catch (caught) {
      setActionError({ action, message: describeError(caught), problems: replyProblems(caught) ?? [] });
      reload();
    } finally {
      setBusy(false);
      setConfirming(null);
    }
  }

  const sendReply = () =>
    run("reply", async () => {
      await api.request<EscalationActionResponse>(`/reviewer/escalations/${ticketId}/reply`, {
        method: "POST",
        body: { text: reply.trim() },
      });
      setReply("");
      toast.success("Reply sent", "The customer sees it on their request now.");
      reload();
    });

  const propose = () =>
    run("propose", async () => {
      const proposal = await api.request<ProposalResponse>(`/tickets/${ticketId}/refund-proposals`, { method: "POST" });
      toast.success("Refund proposed", "Another reviewer must approve it before any money moves.");
      void navigate(`/approvals/${proposal.approval_id}`);
    });

  const resolve = () =>
    run("resolve", async () => {
      await api.request<EscalationActionResponse>(`/reviewer/escalations/${ticketId}/resolve`, {
        method: "POST",
        body: { note: note.trim() },
      });
      toast.success("Ticket resolved", "It has left the escalations inbox.");
      void navigate("/escalations");
    });

  if (error !== null) return <ErrorState message={error} onRetry={reload} />;
  if (loading || data === null) {
    return (
      <>
        <PageHeader title="Escalated case" description="Loading the case…" />
        <DetailSkeleton />
      </>
    );
  }

  const stillEscalated = data.ticket_status === "escalated";
  const replied = data.reply_from === "specialist";
  const refundable = data.facts?.refundable_balance_minor ?? 0;
  const errorFor = (action: Action) =>
    actionError?.action !== action ? null : actionError.problems.length > 0 ? (
      // The guardrail refused the reply: say exactly what to fix.
      <Alert tone="danger" title={actionError.message}>
        <ul className="list-disc space-y-0.5 pl-4">
          {actionError.problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      </Alert>
    ) : (
      <Alert tone="danger">{actionError.message}</Alert>
    );

  return (
    <>
      <PageHeader
        title="Escalated case"
        description={
          <>
            {data.customer_name} · {data.order_number ?? "No order"} · escalated{" "}
            <time dateTime={data.escalated_at} title={formatDateTime(data.escalated_at)}>
              {formatRelative(data.escalated_at)}
            </time>
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

      <section
        aria-label="Why it was escalated"
        data-testid="escalation-reasons"
        className="mb-4 flex items-start gap-3 rounded-xl border border-warning-200 bg-warning-50 px-4 py-3.5 sm:px-5"
      >
        <TriangleAlert className="mt-0.5 size-5 shrink-0 text-warning-600" aria-hidden="true" />
        <div className="min-w-0">
          <h2 className="text-base font-semibold text-warning-900">Why it was escalated</h2>
          <ul className="mt-1 list-disc space-y-0.5 pl-4 text-sm text-warning-900">
            {data.reasons.map((reason) => (
              <li key={reason.code}>{escalationText(reason.code, reason.codes)}</li>
            ))}
          </ul>
        </div>
      </section>

      <div className="grid grid-cols-[minmax(0,1fr)] items-start gap-4 lg:grid-cols-[minmax(0,1fr)_22rem] lg:grid-rows-[auto_1fr] lg:gap-6">
        <div className="space-y-4">
          <Card>
            <CardContent className="p-4 sm:p-5">
              <p className="mb-2 text-xs font-medium text-gray-500">
                Customer&apos;s complaint <span className="font-normal">· their own words, shown as written</span>
              </p>
              <blockquote className="flex gap-3">
                <Quote className="mt-0.5 size-4 shrink-0 text-gray-400" aria-hidden="true" />
                <p className="text-sm leading-relaxed whitespace-pre-line text-gray-800">{data.complaint}</p>
              </blockquote>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4 sm:p-5">
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                <p className="text-xs font-medium text-gray-500">What the customer sees now</p>
                {replied ? (
                  <Badge tone="info">
                    <Headset aria-hidden="true" />
                    Specialist reply
                  </Badge>
                ) : (
                  <Badge tone="neutral">Assistant&apos;s message</Badge>
                )}
              </div>
              <p className="text-sm leading-relaxed whitespace-pre-line text-gray-800" data-testid="current-reply">
                {data.reply ?? "No message yet."}
              </p>
            </CardContent>
          </Card>
        </div>

        <aside aria-label="Actions" className="space-y-4 lg:sticky lg:top-20 lg:col-start-2 lg:row-span-2 lg:row-start-1">
          <Card>
            <CardContent className="space-y-5">
              <div className="flex items-center justify-between gap-3">
                <h2 className="text-base font-semibold text-gray-900">Actions</h2>
                <StatusBadge status={data.ticket_status} label={ticketStatusLabel(data.ticket_status)} />
              </div>
              {!stillEscalated ? (
                <Alert tone="info">This ticket is no longer escalated. Nothing else to do here.</Alert>
              ) : (
                <>
                  <div className="space-y-2">
                    <Label htmlFor="specialist-reply">Reply to the customer</Label>
                    <Textarea
                      id="specialist-reply"
                      rows={4}
                      maxLength={MAX_REPLY_LENGTH}
                      placeholder="What you found and what happens next"
                      aria-describedby="specialist-reply-hint"
                      value={reply}
                      onChange={(event) => {
                        setReply(event.target.value);
                      }}
                    />
                    <FieldHint id="specialist-reply-hint">
                      State only what you have checked. No ids, codes, emails or phone numbers: the guardrail refuses them.
                    </FieldHint>
                    {errorFor("reply")}
                    <Button
                      className="w-full"
                      disabled={busy || reply.trim() === ""}
                      onClick={() => {
                        setConfirming("reply");
                      }}
                    >
                      <Send aria-hidden="true" />
                      Send reply
                    </Button>
                  </div>

                  {data.order_number !== null && (
                    <div className="space-y-2 border-t border-gray-100 pt-5">
                      <p className="text-sm font-medium text-gray-900">Propose a refund</p>
                      <p className="text-xs text-gray-600">
                        The refund rules compute the amount
                        {data.facts?.eligible_now === true && (
                          <>
                            {" "}
                            (<span className="font-medium text-gray-900">{formatMoney(refundable, data.currency ?? "INR")}</span> now)
                          </>
                        )}
                        . Another reviewer must approve it.
                      </p>
                      {errorFor("propose")}
                      <Button
                        variant="outline"
                        className="w-full"
                        disabled={busy || data.facts?.eligible_now !== true}
                        title={data.facts?.eligible_now === true ? undefined : "The order is not eligible for a refund now"}
                        onClick={() => {
                          setConfirming("propose");
                        }}
                      >
                        <ReceiptText aria-hidden="true" />
                        Propose refund
                      </Button>
                      {data.facts?.eligible_now !== true && (
                        <p className="text-xs text-gray-500">Not eligible now. See Order facts for the reasons.</p>
                      )}
                    </div>
                  )}

                  <div className="space-y-2 border-t border-gray-100 pt-5">
                    <Label htmlFor="resolve-note">Resolve the ticket</Label>
                    <Textarea
                      id="resolve-note"
                      rows={2}
                      maxLength={MAX_NOTE_LENGTH}
                      placeholder="Internal note: what you did"
                      aria-describedby="resolve-note-hint"
                      value={note}
                      onChange={(event) => {
                        setNote(event.target.value);
                      }}
                    />
                    <FieldHint id="resolve-note-hint">
                      {replied ? "Kept in the audit trail. Never shown to the customer." : "Send the customer a reply first, so they know the outcome."}
                    </FieldHint>
                    {errorFor("resolve")}
                    <Button
                      variant="outline"
                      className="w-full"
                      disabled={busy || !replied || note.trim() === ""}
                      onClick={() => {
                        setConfirming("resolve");
                      }}
                    >
                      Resolve ticket
                    </Button>
                  </div>
                </>
              )}
            </CardContent>
          </Card>
        </aside>

        <div className="space-y-3 lg:col-start-1">
          <h2 className="pt-2 text-xs font-medium tracking-wide text-gray-500 uppercase">Details</h2>
          <AiAnalysisSections runId={data.run_id} isOpen={(section) => open.has(section)} onOpenChange={setSection} />
          <CollapsibleSection
            id="order-facts"
            title="Order facts"
            icon={Package}
            summary={
              data.order_number === null
                ? "This ticket names no order"
                : data.facts === null
                  ? "The order could not be read now"
                  : "Read just now, rules applied now"
            }
            badge={data.facts === null ? undefined : <EligibleBadge facts={data.facts} />}
            open={open.has("order-facts")}
            onOpenChange={(isOpen) => {
              setSection("order-facts", isOpen);
            }}
          >
            {data.facts === null ? (
              <p className="text-sm text-gray-500">No order facts to show.</p>
            ) : (
              <OrderFactsBody facts={data.facts} currency={data.currency ?? "INR"} policy={data.governing_policy} />
            )}
          </CollapsibleSection>
        </div>
      </div>

      <ConfirmDialog
        open={confirming === "reply"}
        title="Send this reply to the customer?"
        description={
          <>
            <span className="block">{data.customer_name} will see this on their request, from a support specialist:</span>
            <span className="mt-3 flex gap-2 rounded-lg bg-gray-50 px-3 py-2.5 text-gray-800">
              <MessageSquareText className="mt-0.5 size-4 shrink-0 text-gray-400" aria-hidden="true" />
              <span className="line-clamp-6 whitespace-pre-line">{reply.trim()}</span>
            </span>
          </>
        }
        confirmLabel="Send reply"
        busy={busy}
        onConfirm={() => void sendReply()}
        onCancel={() => {
          setConfirming(null);
        }}
      />
      <ConfirmDialog
        open={confirming === "propose"}
        title="Propose a refund?"
        description={
          <>
            The refund rules compute the amount from {data.order_number ?? "the order"}&apos;s records
            {data.facts?.eligible_now === true && <> ({formatMoney(refundable, data.currency ?? "INR")} now)</>}. Nothing is refunded
            yet: another reviewer must approve the proposal, and you cannot approve your own.
          </>
        }
        confirmLabel="Propose refund"
        busy={busy}
        onConfirm={() => void propose()}
        onCancel={() => {
          setConfirming(null);
        }}
      />
      <ConfirmDialog
        open={confirming === "resolve"}
        title="Resolve this ticket?"
        description={
          <>
            The ticket is closed and leaves the escalations inbox. {data.customer_name} keeps seeing your last reply. Your
            note is kept in the audit trail only.
          </>
        }
        confirmLabel="Resolve ticket"
        busy={busy}
        onConfirm={() => void resolve()}
        onCancel={() => {
          setConfirming(null);
        }}
      />
    </>
  );
}
