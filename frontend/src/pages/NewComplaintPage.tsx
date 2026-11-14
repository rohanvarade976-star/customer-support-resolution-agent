// A new complaint: create the ticket (POST /tickets), start its run (POST /tickets/{id}/runs),
// then open the run's live page. Who the customer is comes from the token, never this form.

import { useState, type SyntheticEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { describeError } from "../api/client";
import type { Order, Run, Ticket } from "../api/types";
import { useAuth } from "../auth/auth";
import { buttonClass, Card, ErrorBanner, inputClass, PageTitle } from "../components/ui";
import { useApiGet } from "../hooks/useApiGet";

const MAX_LENGTH = 2000; // the API's limit (schemas/tickets.py)

export function NewComplaintPage() {
  const { api } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const orders = useApiGet<Order[]>("/me/orders");
  const [orderNumber, setOrderNumber] = useState(params.get("order") ?? "");
  const [message, setMessage] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [createdTicket, setCreatedTicket] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: SyntheticEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    let ticketId: string | null = null;
    try {
      const body = orderNumber === "" ? { message } : { message, order_number: orderNumber };
      const ticket = await api.request<Ticket>("/tickets", { method: "POST", body });
      ticketId = ticket.id;
      const run = await api.request<Run>(`/tickets/${ticket.id}/runs`, { method: "POST" });
      void navigate(`/runs/${run.run_id}`);
    } catch (caught) {
      setCreatedTicket(ticketId); // the ticket exists even if starting its run failed
      setError(describeError(caught));
      setBusy(false);
    }
  }

  return (
    <>
      <PageTitle>New complaint</PageTitle>
      <Card>
        <form onSubmit={(event) => void submit(event)} className="space-y-4">
          <div>
            <label htmlFor="order" className="mb-1 block text-sm font-medium">
              Order (optional)
            </label>
            <select
              id="order"
              className={inputClass}
              value={orderNumber}
              onChange={(event) => {
                setOrderNumber(event.target.value);
              }}
            >
              <option value="">No specific order</option>
              {orders.data?.map((order) => (
                <option key={order.order_number} value={order.order_number}>
                  {order.order_number} · {order.items.map((item) => item.product_name).join(", ")}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="message" className="mb-1 block text-sm font-medium">
              What went wrong?
            </label>
            <textarea
              id="message"
              required
              rows={5}
              maxLength={MAX_LENGTH}
              className={inputClass}
              value={message}
              onChange={(event) => {
                setMessage(event.target.value);
              }}
            />
            <p className="mt-1 text-xs text-slate-500">
              {message.length}/{MAX_LENGTH}
            </p>
          </div>
          <ErrorBanner message={error ?? orders.error} />
          {createdTicket !== null && (
            <p className="text-sm">
              Your ticket was saved. You can start it again from{" "}
              <Link to="/tickets" className="text-indigo-700 underline">
                My tickets
              </Link>
              .
            </p>
          )}
          <button type="submit" className={buttonClass} disabled={busy || message.trim() === ""}>
            {busy ? "Sending…" : "Send complaint"}
          </button>
        </form>
      </Card>
    </>
  );
}
