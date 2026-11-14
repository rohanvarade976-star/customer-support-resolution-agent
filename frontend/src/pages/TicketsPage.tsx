// The customer's own tickets (GET /tickets), each with a link to its latest run. A ticket
// whose run never started (or whose last run ended) can be started again.

import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { describeError } from "../api/client";
import type { Run, TicketList } from "../api/types";
import { useAuth } from "../auth/auth";
import { Card, ErrorBanner, Loading, PageTitle, secondaryButtonClass, StatusBadge } from "../components/ui";
import { useApiGet } from "../hooks/useApiGet";
import { formatDateTime, runStatusLabel } from "../lib/labels";

const FINISHED_RUNS = new Set(["completed", "escalated", "failed"]);

export function TicketsPage() {
  const { api } = useAuth();
  const navigate = useNavigate();
  const { data, error, loading } = useApiGet<TicketList>("/tickets?limit=50");
  const [actionError, setActionError] = useState<string | null>(null);

  async function startRun(ticketId: string) {
    setActionError(null);
    try {
      const run = await api.request<Run>(`/tickets/${ticketId}/runs`, { method: "POST" });
      void navigate(`/runs/${run.run_id}`);
    } catch (caught) {
      setActionError(describeError(caught));
    }
  }

  return (
    <>
      <PageTitle>My tickets</PageTitle>
      <ErrorBanner message={error ?? actionError} />
      {loading && <Loading />}
      {data?.items.length === 0 && (
        <p className="text-sm text-slate-600">
          No tickets yet.{" "}
          <Link to="/complaints/new" className="text-indigo-700 underline">
            Raise a complaint
          </Link>
          .
        </p>
      )}
      {data?.items.map((ticket) => (
        <Card key={ticket.id}>
          <div className="flex flex-wrap items-center gap-3">
            <StatusBadge status={ticket.status} />
            {ticket.order_number !== null && <span className="font-medium">{ticket.order_number}</span>}
            <span className="ml-auto text-xs text-slate-500">{formatDateTime(ticket.created_at)}</span>
          </div>
          {/* The customer's own words, shown as plain text. */}
          <p className="mt-2 line-clamp-2 text-sm text-slate-700">{ticket.summary}</p>
          <div className="mt-2 flex items-center gap-3 text-sm">
            {ticket.latest_run_id !== null && ticket.latest_run_status !== null && (
              <Link to={`/runs/${ticket.latest_run_id}`} className="text-indigo-700 underline">
                View progress ({runStatusLabel(ticket.latest_run_status)})
              </Link>
            )}
            {(ticket.latest_run_status === null || FINISHED_RUNS.has(ticket.latest_run_status)) &&
              ticket.status !== "resolved" && (
                <button
                  type="button"
                  className={secondaryButtonClass}
                  onClick={() => void startRun(ticket.id)}
                >
                  {ticket.latest_run_status === null ? "Start" : "Start again"}
                </button>
              )}
          </div>
        </Card>
      ))}
    </>
  );
}
