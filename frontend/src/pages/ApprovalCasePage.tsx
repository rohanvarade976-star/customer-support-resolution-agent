// Reviewers: one approval's case (GET /approvals/{id}). Evidence, policy citations and the
// stored amount, all from the API; approve or reject with a note; then the refund's status.
// The page computes nothing: the amount shown is the stored proposal's, the facts and
// eligibility come from the backend's rules.

import { useEffect, useState, type SyntheticEvent } from "react";
import { Link, useParams } from "react-router-dom";

import { describeError } from "../api/client";
import type { ApprovalCase, CaseFacts, DecisionResponse } from "../api/types";
import { useAuth } from "../auth/auth";
import {
  buttonClass,
  Card,
  ErrorBanner,
  inputClass,
  Loading,
  PageTitle,
  secondaryButtonClass,
  StatusBadge,
} from "../components/ui";
import { useApiGet } from "../hooks/useApiGet";
import { formatDateTime, humanize } from "../lib/labels";
import { formatMoney } from "../lib/money";

const MAX_NOTE_LENGTH = 1000; // the API's limit (schemas/approvals.py)
const SETTLED_REFUNDS = new Set(["succeeded", "failed"]);

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
  const { data, error, loading, reload } = useApiGet<ApprovalCase>(`/approvals/${approvalId}`);
  const [note, setNote] = useState("");
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

  async function decide(event: SyntheticEvent, decision: "approve" | "reject") {
    event.preventDefault();
    setBusy(true);
    setActionError(null);
    try {
      const body = note.trim() === "" ? { decision } : { decision, note: note.trim() };
      const response = await api.request<DecisionResponse>(`/approvals/${approvalId}/decision`, {
        method: "POST",
        body,
      });
      setResult(response);
      reload();
    } catch (caught) {
      setActionError(describeError(caught));
      reload(); // e.g. already decided or expired: show the current state
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Link to="/approvals" className="text-sm text-indigo-700 underline">
        ← Approval queue
      </Link>
      <PageTitle>Refund case</PageTitle>
      <ErrorBanner message={error ?? actionError} />
      {loading && <Loading />}
      {data !== null && (
        <>
          <Card>
            <div className="flex flex-wrap items-center gap-3">
              <span className="font-medium">{data.customer_name}</span>
              <span>{data.proposal.order_number}</span>
              <StatusBadge status={data.approval_status} />
              <span className="ml-auto text-2xl font-semibold" data-testid="amount">
                {formatMoney(data.proposal.amount_minor, data.proposal.currency)}
              </span>
            </div>
            <p className="mt-1 text-xs text-slate-500">
              Amount computed by the refund rules when the proposal was made. Expires{" "}
              {formatDateTime(data.expires_at)}.
            </p>
            <h2 className="mt-3 text-sm font-medium text-slate-600">Complaint (customer&apos;s words)</h2>
            <p className="mt-1 whitespace-pre-line text-sm">{data.complaint}</p>
            <h2 className="mt-3 text-sm font-medium text-slate-600">Checks passed</h2>
            <ul className="mt-1 flex flex-wrap gap-1">
              {data.proposal.checks_passed.map((check) => (
                <li key={check} className="rounded bg-emerald-50 px-2 py-0.5 text-xs text-emerald-800">
                  {humanize(check)}
                </li>
              ))}
            </ul>
            <Link to={`/runs/${data.run_id}`} className="mt-3 inline-block text-sm text-indigo-700 underline">
              Open the run&apos;s live timeline
            </Link>
          </Card>

          {data.approval_status === "pending" ? (
            <Card title="Decision">
              <form className="space-y-2">
                <label htmlFor="note" className="block text-sm font-medium">
                  Note (optional, kept in the audit trail)
                </label>
                <textarea
                  id="note"
                  rows={2}
                  maxLength={MAX_NOTE_LENGTH}
                  className={inputClass}
                  value={note}
                  onChange={(event) => {
                    setNote(event.target.value);
                  }}
                />
                <div className="flex gap-2">
                  <button
                    type="submit"
                    className={buttonClass}
                    disabled={busy}
                    onClick={(event) => void decide(event, "approve")}
                  >
                    Approve refund
                  </button>
                  <button
                    type="submit"
                    className={secondaryButtonClass}
                    disabled={busy}
                    onClick={(event) => void decide(event, "reject")}
                  >
                    Reject
                  </button>
                </div>
              </form>
            </Card>
          ) : (
            <Card title="Outcome">
              {result !== null && <p className="mb-2 text-sm">{result.message}</p>}
              <p className="text-sm">
                Decision: <StatusBadge status={data.approval_status} />{" "}
                {data.decided_at !== null && (
                  <span className="text-xs text-slate-500">{formatDateTime(data.decided_at)}</span>
                )}
              </p>
              <p className="mt-2 text-sm" data-testid="refund-status">
                Refund:{" "}
                {data.refund === null ? (
                  data.approval_status === "approved" ? (
                    "being processed…"
                  ) : (
                    "none"
                  )
                ) : (
                  <>
                    <StatusBadge status={data.refund.status} />{" "}
                    {formatMoney(data.refund.amount_minor, data.proposal.currency)}
                    {data.refund.provider_reference !== null && (
                      <span className="ml-2 font-mono text-xs text-slate-500">
                        {data.refund.provider_reference}
                      </span>
                    )}
                  </>
                )}
              </p>
              {waiting && <p className="mt-1 text-xs text-slate-500">Checking for updates…</p>}
            </Card>
          )}

          {data.facts !== null && <FactsCard facts={data.facts} currency={data.proposal.currency} />}

          <Card title="Policy">
            {data.governing_policy !== null && (
              <p className="mb-2 text-sm">
                In force for this order: <strong>{data.governing_policy.title}</strong>{" "}
                {data.governing_policy.version}
                {data.governing_policy.refund_window_days !== null &&
                  ` · ${data.governing_policy.refund_window_days}-day return window`}
              </p>
            )}
            {data.citations.length === 0 ? (
              <p className="text-sm text-slate-500">The run cited no policy sections.</p>
            ) : (
              data.citations.map((citation) => (
                <details key={citation.citation} className="mb-2 rounded border border-slate-200 p-2">
                  <summary className="cursor-pointer text-sm">
                    {citation.policy_title} {citation.version} · {citation.section_title}{" "}
                    <code className="text-xs text-slate-500">{citation.citation}</code>
                  </summary>
                  <p className="mt-2 whitespace-pre-line text-sm text-slate-700">{citation.text}</p>
                </details>
              ))
            )}
          </Card>
        </>
      )}
    </>
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
    <Card title="Facts (from the order records, now)">
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
        {rows.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-slate-500">{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
      <p className="mt-2 text-sm">
        Eligible now:{" "}
        {facts.eligible_now ? (
          <StatusBadge status="approved" label="Yes" />
        ) : (
          <>
            <StatusBadge status="rejected" label="No" />{" "}
            <span className="text-xs text-slate-600">{facts.reasons_now.map(humanize).join(", ")}</span>
          </>
        )}
      </p>
    </Card>
  );
}
