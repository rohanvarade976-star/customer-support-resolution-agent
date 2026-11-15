// One ticket in a list (home page and "My tickets"): its status, order, the customer's own
// words, and the next thing to do: reply, view progress, or start it (again). An escalated
// ticket says a specialist has it, and shows the specialist's reply once there is one (ADR-022).

import { Headset, MessageSquareText, RotateCcw } from "lucide-react";
import { Link } from "react-router-dom";

import type { RunStatus, Ticket } from "../api/types";
import { humanize, runStatusLabel, ticketStatusLabel } from "../lib/labels";
import { formatRelative } from "../lib/time";
import { StatusBadge } from "./StatusBadge";
import { Button } from "./ui/button";

const FINISHED_RUNS = new Set<RunStatus>(["completed", "escalated", "failed"]);

// A ticket whose run never started, or whose last run ended without resolving it, can be
// started again (the API decides whether that is allowed).
function canStart(ticket: Ticket): boolean {
  return (
    (ticket.latest_run_status === null || FINISHED_RUNS.has(ticket.latest_run_status)) &&
    ticket.status !== "resolved" &&
    ticket.status !== "escalated" // a specialist has it: starting again would only add noise
  );
}

interface TicketListItemProps {
  ticket: Ticket;
  onStart?: (ticketId: string) => void;
  starting?: boolean;
}

export function TicketListItem({ ticket, onStart, starting = false }: TicketListItemProps) {
  const runId = ticket.latest_run_id;
  const runStatus = ticket.latest_run_status;
  const awaitingYou = runStatus === "awaiting_customer";

  return (
    <li className="flex flex-col gap-3 px-5 py-4 transition-colors hover:bg-gray-50/70 sm:flex-row sm:items-center sm:gap-4">
      <div className="flex min-w-0 flex-1 gap-4">
        <span className="hidden size-10 shrink-0 place-items-center rounded-lg bg-gray-100 text-gray-500 sm:grid">
          <MessageSquareText className="size-5" aria-hidden="true" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <StatusBadge status={ticket.status} label={ticketStatusLabel(ticket.status)} />
            <span className="text-sm font-medium text-gray-900">{ticket.order_number ?? "General question"}</span>
            {ticket.category !== null && <span className="text-xs text-gray-500">· {humanize(ticket.category)}</span>}
          </div>
          {/* The customer's own words, shown as plain text. */}
          <p className="mt-1 line-clamp-2 text-sm text-gray-700">{ticket.summary}</p>
          <p className="mt-1 text-xs text-gray-500">
            Raised {formatRelative(ticket.created_at)}
            {runStatus !== null && !awaitingYou && ticket.status !== "escalated" && ` · ${runStatusLabel(runStatus)}`}
          </p>
          {ticket.status === "escalated" && ticket.reply_from !== "specialist" && (
            <p className="mt-2 flex items-center gap-1.5 text-xs text-info-800">
              <Headset className="size-3.5 shrink-0" aria-hidden="true" />
              A specialist is looking at this. Their reply will appear here.
            </p>
          )}
          {ticket.reply_from === "specialist" && ticket.reply !== null && (
            <div className="mt-2 rounded-lg border border-primary-100 bg-primary-50/60 px-3 py-2" data-testid="ticket-specialist-reply">
              <p className="flex items-center gap-1.5 text-xs font-medium text-primary-900">
                <Headset className="size-3.5 shrink-0" aria-hidden="true" />
                Reply from a support specialist
              </p>
              <p className="mt-1 line-clamp-3 text-sm whitespace-pre-line text-gray-800">{ticket.reply}</p>
            </div>
          )}
        </div>
      </div>
      <div className="flex shrink-0 flex-wrap gap-2 sm:justify-end">
        {runId !== null && runStatus !== null && (
          <Button asChild size="sm" variant={awaitingYou ? "primary" : "outline"}>
            <Link to={`/runs/${runId}`}>{awaitingYou ? "Reply now" : "View progress"}</Link>
          </Button>
        )}
        {onStart !== undefined && canStart(ticket) && (
          <Button
            size="sm"
            variant="outline"
            loading={starting}
            onClick={() => {
              onStart(ticket.id);
            }}
          >
            {!starting && <RotateCcw aria-hidden="true" />}
            {runStatus === null ? "Start" : "Start again"}
          </Button>
        )}
      </div>
    </li>
  );
}
