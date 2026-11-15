// The customer's own tickets (GET /tickets), searchable and filterable by status, each with its
// next action. A ticket whose run never started (or whose last run ended) can be started again.
// Search and filters only narrow the list the API returned.

import { MessageSquareText, Plus, Search, SearchX } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { describeError } from "../api/client";
import type { Run, Ticket, TicketList } from "../api/types";
import { useAuth } from "../auth/auth";
import { PageHeader } from "../components/PageHeader";
import { EmptyState, ErrorState, ListSkeleton } from "../components/states";
import { TicketListItem } from "../components/TicketListItem";
import { Button } from "../components/ui/button";
import { Card } from "../components/ui/card";
import { Input } from "../components/ui/form";
import { useToast } from "../components/ui/toast-context";
import { useApiGet } from "../hooks/useApiGet";
import { cn } from "../lib/utils";

type Filter = "all" | "open" | "awaiting" | "resolved";

const FILTERS: { key: Filter; label: string; matches: (ticket: Ticket) => boolean }[] = [
  { key: "all", label: "All", matches: () => true },
  { key: "open", label: "Open", matches: (t) => t.status !== "resolved" },
  { key: "awaiting", label: "Waiting on you", matches: (t) => t.status === "awaiting_customer" },
  { key: "resolved", label: "Resolved", matches: (t) => t.status === "resolved" },
];

export function TicketsPage() {
  const { api } = useAuth();
  const toast = useToast();
  const navigate = useNavigate();
  const { data, error, loading, reload } = useApiGet<TicketList>("/tickets?limit=50");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [starting, setStarting] = useState<string | null>(null);

  async function startRun(ticketId: string) {
    setStarting(ticketId);
    try {
      const run = await api.request<Run>(`/tickets/${ticketId}/runs`, { method: "POST" });
      void navigate(`/runs/${run.run_id}`);
    } catch (caught) {
      toast.error("Couldn't start the request", describeError(caught));
      setStarting(null);
    }
  }

  const items = data?.items ?? [];
  const needle = query.trim().toLowerCase();
  const active = FILTERS.find((f) => f.key === filter) ?? FILTERS[0];
  const shown = items.filter(
    (ticket) =>
      (active?.matches(ticket) ?? true) &&
      (needle === "" ||
        ticket.summary.toLowerCase().includes(needle) ||
        (ticket.order_number ?? "").toLowerCase().includes(needle)),
  );

  return (
    <>
      <PageHeader
        title="My tickets"
        description="Every request you've raised and where it stands."
        actions={
          <Button asChild>
            <Link to="/complaints/new">
              <Plus aria-hidden="true" />
              New request
            </Link>
          </Button>
        }
      />

      {error !== null ? (
        <ErrorState message={error} onRetry={reload} />
      ) : loading ? (
        <ListSkeleton rows={4} />
      ) : items.length === 0 ? (
        <Card>
          <EmptyState
            icon={MessageSquareText}
            title="No tickets yet"
            description="Start a request about an order and we'll investigate it for you."
            action={
              <Button asChild>
                <Link to="/complaints/new">Start a request</Link>
              </Button>
            }
          />
        </Card>
      ) : (
        <Card>
          <div className="flex flex-col gap-3 border-b border-gray-100 p-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="relative sm:w-72">
              <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-gray-400" aria-hidden="true" />
              <label htmlFor="ticket-search" className="sr-only">
                Search tickets
              </label>
              <Input
                id="ticket-search"
                type="search"
                placeholder="Search by order or words"
                className="pl-9"
                value={query}
                onChange={(event) => {
                  setQuery(event.target.value);
                }}
              />
            </div>
            <div role="group" aria-label="Filter by status" className="grid grid-cols-2 gap-1 rounded-lg bg-gray-100 p-1 sm:flex">
              {FILTERS.map((f) => (
                <button
                  key={f.key}
                  type="button"
                  aria-pressed={filter === f.key}
                  onClick={() => {
                    setFilter(f.key);
                  }}
                  className={cn(
                    "cursor-pointer rounded-md px-3 py-1.5 text-sm font-medium whitespace-nowrap transition-colors",
                    filter === f.key ? "bg-white text-gray-900 shadow-xs" : "text-gray-600 hover:text-gray-900",
                  )}
                >
                  {f.label}
                  <span className="ml-1.5 text-xs text-gray-500 tabular-nums">{items.filter(f.matches).length}</span>
                </button>
              ))}
            </div>
          </div>
          {shown.length === 0 ? (
            <EmptyState
              icon={SearchX}
              title="No tickets match"
              description="Try different words or another status."
              action={
                <Button
                  variant="outline"
                  onClick={() => {
                    setQuery("");
                    setFilter("all");
                  }}
                >
                  Clear filters
                </Button>
              }
            />
          ) : (
            <ul className="divide-y divide-gray-100">
              {shown.map((ticket) => (
                <TicketListItem
                  key={ticket.id}
                  ticket={ticket}
                  onStart={(id) => void startRun(id)}
                  starting={starting === ticket.id}
                />
              ))}
            </ul>
          )}
        </Card>
      )}
    </>
  );
}
