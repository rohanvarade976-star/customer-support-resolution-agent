// A new request as a guided wizard: 1. which order, 2. what went wrong, 3. describe it, then a
// review step. Submitting creates the ticket (POST /tickets), starts its run
// (POST /tickets/{id}/runs) and opens the run's live page.
//
// The API takes only `message` and `order_number` (anything else is refused), and who the
// customer is comes from the token. So step 2's choice only starts the description with an
// editable sentence; the backend's classifier decides the category from the customer's words.

import {
  Check,
  CircleQuestionMark,
  PackageX,
  Send,
  Truck,
  Wallet,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useRef, useState, type ReactNode, type SyntheticEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { describeError } from "../api/client";
import type { Order, Run, Ticket } from "../api/types";
import { useAuth } from "../auth/auth";
import { PageHeader } from "../components/PageHeader";
import { ErrorState } from "../components/states";
import { StatusBadge } from "../components/StatusBadge";
import { Alert } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Card, CardFooter } from "../components/ui/card";
import { FieldError, FieldHint, Label, Textarea } from "../components/ui/form";
import { Skeleton } from "../components/ui/skeleton";
import { useApiGet } from "../hooks/useApiGet";
import { formatDateTime } from "../lib/labels";
import { formatMoney } from "../lib/money";
import { orderItemsSummary } from "../lib/orders";
import { cn } from "../lib/utils";

const MAX_LENGTH = 2000; // the API's limit (schemas/tickets.py)
const STEPS = ["Order", "Problem", "Details", "Review"];
const NO_ORDER = ""; // "Not about a specific order"

interface Issue {
  key: string;
  icon: LucideIcon;
  title: string;
  text: string;
  starter: string; // the sentence the description starts with; the customer can change it
}

const ISSUES: Issue[] = [
  {
    key: "where",
    icon: Truck,
    title: "Where is my order?",
    text: "Delivery updates or a delay",
    starter: "I'd like an update on where my order is.",
  },
  {
    key: "refund",
    icon: Wallet,
    title: "Refund not received",
    text: "You returned something or were promised a refund",
    starter: "I returned my item but haven't received my refund yet.",
  },
  {
    key: "damaged",
    icon: PackageX,
    title: "Damaged or wrong item",
    text: "It arrived broken, faulty or not what you ordered",
    starter: "The item I received was damaged or not what I ordered.",
  },
  {
    key: "other",
    icon: CircleQuestionMark,
    title: "Something else",
    text: "Tell us in your own words",
    starter: "",
  },
];

export function NewComplaintPage() {
  const { api } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const orders = useApiGet<Order[]>("/me/orders");

  const [step, setStep] = useState(0);
  const [orderNumber, setOrderNumber] = useState<string | null>(params.get("order")); // null: not chosen
  // ?issue= (from a run's "Report a problem" action) pre-selects the problem; unknown keys are ignored.
  const [issueKey, setIssueKey] = useState<string | null>(() => {
    const wanted = params.get("issue");
    return ISSUES.some((candidate) => candidate.key === wanted) ? wanted : null;
  });
  const [message, setMessage] = useState("");
  const [starterUsed, setStarterUsed] = useState(""); // the starter sentence we last filled in
  const [stepError, setStepError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [createdTicket, setCreatedTicket] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // Move focus to the new step's heading, so keyboard and screen reader users start there.
  const headingRef = useRef<HTMLHeadingElement>(null);
  const firstRender = useRef(true);
  useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false;
      return;
    }
    headingRef.current?.focus();
  }, [step]);

  const issue = ISSUES.find((candidate) => candidate.key === issueKey) ?? null;
  const order = orders.data?.find((candidate) => candidate.order_number === orderNumber) ?? null;

  function goTo(next: number) {
    setStepError(null);
    setStep(next);
  }

  function next(event: SyntheticEvent) {
    event.preventDefault();
    // An order from the link (?order=) that is not in the customer's list counts as not chosen.
    const unknownOrder = orderNumber !== null && orderNumber !== NO_ORDER && order === null;
    if (step === 0 && (orderNumber === null || unknownOrder)) {
      setStepError("Choose an order, or pick “Not about a specific order”.");
      return;
    }
    if (step === 1) {
      if (issue === null) {
        setStepError("Choose the option closest to your problem.");
        return;
      }
      // Start the description with the chosen sentence, unless the customer already wrote their own.
      if (message.trim() === "" || message === starterUsed) {
        setMessage(issue.starter);
        setStarterUsed(issue.starter);
      }
    }
    if (step === 2 && message.trim() === "") {
      setStepError("Please describe what happened, so we know where to start.");
      return;
    }
    if (step === 3) {
      void submit();
      return;
    }
    goTo(step + 1);
  }

  async function submit() {
    setSubmitting(true);
    setSubmitError(null);
    let ticketId: string | null = null;
    try {
      const text = message.trim();
      const body = orderNumber === null || orderNumber === NO_ORDER ? { message: text } : { message: text, order_number: orderNumber };
      const ticket = await api.request<Ticket>("/tickets", { method: "POST", body });
      ticketId = ticket.id;
      const run = await api.request<Run>(`/tickets/${ticket.id}/runs`, { method: "POST" });
      void navigate(`/runs/${run.run_id}`);
    } catch (caught) {
      setCreatedTicket(ticketId); // the ticket exists even if starting its run failed
      setSubmitError(describeError(caught));
      setSubmitting(false);
    }
  }

  const errorId = "step-error";

  return (
    <div className="mx-auto max-w-3xl">
      <PageHeader title="New request" description="Tell us what happened. It takes about a minute." />
      <StepIndicator current={step} />

      <Card className="mt-6">
        <form onSubmit={next} noValidate>
          <div className="px-5 py-6 sm:px-6">
            <h2 ref={headingRef} tabIndex={-1} id="step-heading" className="text-lg font-semibold text-gray-900 outline-none">
              {["Which order is this about?", "What went wrong?", "Describe what happened", "Review your request"][step]}
            </h2>
            <p className="mt-1 text-sm text-gray-600">
              {
                [
                  "Pick the order, so we can look up its details for you.",
                  "Choose the closest option. You can explain more in the next step.",
                  "A few sentences in your own words is perfect.",
                  "Check everything looks right, then send it to us.",
                ][step]
              }
            </p>

            <div className="mt-6">
              {step === 0 && (
                <OrderStep
                  orders={orders.data}
                  loading={orders.loading}
                  error={orders.error}
                  onRetry={orders.reload}
                  value={orderNumber}
                  invalid={stepError !== null}
                  errorId={errorId}
                  onChange={(value) => {
                    setOrderNumber(value);
                    setStepError(null);
                  }}
                />
              )}
              {step === 1 && (
                <fieldset aria-labelledby="step-heading" aria-describedby={stepError === null ? undefined : errorId}>
                  <div className="grid gap-3 sm:grid-cols-2">
                    {ISSUES.map((candidate) => {
                      const Icon = candidate.icon;
                      return (
                        <RadioCard
                          key={candidate.key}
                          name="issue"
                          value={candidate.key}
                          checked={issueKey === candidate.key}
                          onChange={() => {
                            setIssueKey(candidate.key);
                            setStepError(null);
                          }}
                        >
                          <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-primary-50 text-primary-600">
                            <Icon className="size-4.5" aria-hidden="true" />
                          </span>
                          <span className="min-w-0">
                            <span className="block text-sm font-medium text-gray-900">{candidate.title}</span>
                            <span className="mt-0.5 block text-xs text-gray-600">{candidate.text}</span>
                          </span>
                        </RadioCard>
                      );
                    })}
                  </div>
                </fieldset>
              )}
              {step === 2 && (
                <div>
                  <Label htmlFor="message">Your message</Label>
                  <Textarea
                    id="message"
                    rows={6}
                    maxLength={MAX_LENGTH}
                    aria-invalid={stepError !== null}
                    aria-describedby={`message-hint${stepError === null ? "" : ` ${errorId}`}`}
                    value={message}
                    onChange={(event) => {
                      setMessage(event.target.value);
                      setStepError(null);
                    }}
                  />
                  <FieldHint id="message-hint" className="flex justify-between gap-4">
                    <span>Include dates or what you've already tried. Never share passwords or card numbers.</span>
                    <span className="shrink-0 tabular-nums">
                      {message.length}/{MAX_LENGTH}
                    </span>
                  </FieldHint>
                </div>
              )}
              {step === 3 && (
                <Review
                  orderLabel={orderNumber === NO_ORDER || orderNumber === null ? "Not about a specific order" : orderNumber}
                  orderDetail={order === null ? null : orderItemsSummary(order)}
                  issue={issue?.title ?? "—"}
                  message={message.trim()}
                  onEdit={goTo}
                />
              )}
              <FieldError id={errorId}>{stepError}</FieldError>
              {submitError !== null && (
                <div className="mt-4">
                  <Alert tone="danger" title="We couldn't send your request">
                    {submitError}
                    {createdTicket !== null && (
                      <p className="mt-1">
                        Your ticket was saved. You can start it again from{" "}
                        <Link to="/tickets" className="font-medium underline">
                          My tickets
                        </Link>
                        .
                      </p>
                    )}
                  </Alert>
                </div>
              )}
            </div>
          </div>

          <CardFooter className="justify-between sm:px-6">
            {step === 0 ? (
              <Button asChild variant="ghost">
                <Link to="/home">Cancel</Link>
              </Button>
            ) : (
              <Button
                variant="ghost"
                disabled={submitting}
                onClick={() => {
                  goTo(step - 1);
                }}
              >
                Back
              </Button>
            )}
            {step < 3 ? (
              <Button type="submit">Continue</Button>
            ) : (
              <Button type="submit" loading={submitting}>
                {!submitting && <Send aria-hidden="true" />}
                {submitting ? "Submitting…" : "Submit request"}
              </Button>
            )}
          </CardFooter>
        </form>
      </Card>
    </div>
  );
}

function StepIndicator({ current }: { current: number }) {
  return (
    <nav aria-label="Progress">
      <p className="mb-3 text-sm font-medium text-gray-600 sm:hidden">
        Step {current + 1} of {STEPS.length}: <span className="text-gray-900">{STEPS[current]}</span>
      </p>
      <ol className="flex items-center gap-2 sm:gap-3">
        {STEPS.map((label, i) => {
          const done = i < current;
          const active = i === current;
          return (
            <li key={label} aria-current={active ? "step" : undefined} className="flex flex-1 items-center gap-2 sm:gap-3">
              <span
                className={cn(
                  "grid size-7 shrink-0 place-items-center rounded-full text-xs font-semibold",
                  done && "bg-primary-600 text-white",
                  active && "border-2 border-primary-600 bg-white text-primary-700",
                  !done && !active && "border-2 border-gray-200 bg-white text-gray-500",
                )}
              >
                {done ? <Check className="size-3.5" strokeWidth={3} aria-hidden="true" /> : i + 1}
              </span>
              <span
                className={cn(
                  "hidden text-sm font-medium whitespace-nowrap sm:inline",
                  active ? "text-gray-900" : done ? "text-gray-700" : "text-gray-500",
                )}
              >
                {label}
                <span className="sr-only">{done ? " (done)" : active ? " (current)" : ""}</span>
              </span>
              {i < STEPS.length - 1 && (
                <span aria-hidden="true" className={cn("h-0.5 flex-1 rounded-full", done ? "bg-primary-600" : "bg-gray-200")} />
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

interface RadioCardProps {
  name: string;
  value: string;
  checked: boolean;
  onChange: () => void;
  children: ReactNode;
}

// A radio button that looks like a selectable card. The real <input type="radio"> keeps
// arrow-key navigation and screen reader support; it is visually hidden.
function RadioCard({ name, value, checked, onChange, children }: RadioCardProps) {
  return (
    <label
      className={cn(
        "relative flex cursor-pointer items-start gap-3 rounded-xl border bg-white p-4 pr-10 shadow-xs transition-colors",
        "has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-primary-600",
        checked ? "border-primary-600 bg-primary-50/40 ring-1 ring-primary-600" : "border-gray-200 hover:border-gray-300 hover:bg-gray-50/60",
      )}
    >
      <input type="radio" name={name} value={value} checked={checked} onChange={onChange} className="sr-only" />
      {children}
      <span
        aria-hidden="true"
        className={cn(
          "absolute top-4 right-4 grid size-4.5 place-items-center rounded-full border",
          checked ? "border-primary-600 bg-primary-600 text-white" : "border-gray-300 bg-white",
        )}
      >
        {checked && <Check className="size-3" strokeWidth={3} />}
      </span>
    </label>
  );
}

interface OrderStepProps {
  orders: Order[] | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  value: string | null;
  invalid: boolean;
  errorId: string;
  onChange: (value: string) => void;
}

function OrderStep({ orders, loading, error, onRetry, value, invalid, errorId, onChange }: OrderStepProps) {
  if (error !== null) return <ErrorState message={error} onRetry={onRetry} />;
  if (loading || orders === null) {
    return (
      <div className="grid gap-3 sm:grid-cols-2" aria-busy="true" aria-label="Loading your orders">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="space-y-2 rounded-xl border border-gray-200 p-4">
            <Skeleton className="h-4 w-24" />
            <Skeleton className="h-3 w-40" />
            <Skeleton className="h-3 w-32" />
          </div>
        ))}
      </div>
    );
  }
  return (
    <fieldset aria-labelledby="step-heading" aria-describedby={invalid ? errorId : undefined}>
      <div className="grid gap-3 sm:grid-cols-2">
        {orders.map((order) => (
          <RadioCard
            key={order.order_number}
            name="order"
            value={order.order_number}
            checked={value === order.order_number}
            onChange={() => {
              onChange(order.order_number);
            }}
          >
            <span className="min-w-0 flex-1">
              <span className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-semibold text-gray-900">{order.order_number}</span>
                <StatusBadge status={order.status} />
              </span>
              <span className="mt-1 block truncate text-sm text-gray-700">{orderItemsSummary(order)}</span>
              <span className="mt-1 block text-xs text-gray-500">
                {formatDateTime(order.placed_at)} ·{" "}
                <span className="tabular-nums">{formatMoney(order.paid_amount_minor, order.currency)}</span>
              </span>
            </span>
          </RadioCard>
        ))}
        <RadioCard
          name="order"
          value={NO_ORDER}
          checked={value === NO_ORDER}
          onChange={() => {
            onChange(NO_ORDER);
          }}
        >
          <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-gray-100 text-gray-500">
            <CircleQuestionMark className="size-4.5" aria-hidden="true" />
          </span>
          <span>
            <span className="block text-sm font-medium text-gray-900">Not about a specific order</span>
            <span className="mt-0.5 block text-xs text-gray-600">A general question about your account or our policies</span>
          </span>
        </RadioCard>
      </div>
    </fieldset>
  );
}

interface ReviewProps {
  orderLabel: string;
  orderDetail: string | null;
  issue: string;
  message: string;
  onEdit: (step: number) => void;
}

function Review({ orderLabel, orderDetail, issue, message, onEdit }: ReviewProps) {
  const rows: { label: string; step: number; value: ReactNode }[] = [
    {
      label: "Order",
      step: 0,
      value: (
        <>
          <span className="font-medium text-gray-900">{orderLabel}</span>
          {orderDetail !== null && <span className="block text-gray-600">{orderDetail}</span>}
        </>
      ),
    },
    { label: "Topic", step: 1, value: <span className="text-gray-900">{issue}</span> },
    {
      label: "Your message",
      step: 2,
      value: <span className="whitespace-pre-line text-gray-900">{message}</span>,
    },
  ];
  return (
    <dl className="divide-y divide-gray-100 rounded-xl border border-gray-200">
      {rows.map((row) => (
        <div
          key={row.label}
          className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-4 gap-y-1 px-4 py-3.5 sm:grid-cols-[9rem_minmax(0,1fr)_auto]"
        >
          <dt className="col-start-1 row-start-1 text-sm text-gray-500">{row.label}</dt>
          <dd className="col-span-2 row-start-2 text-sm break-words sm:col-span-1 sm:col-start-2 sm:row-start-1">
            {row.value}
          </dd>
          <dd className="col-start-2 row-start-1 text-right sm:col-start-3">
            <Button
              variant="link"
              size="sm"
              onClick={() => {
                onEdit(row.step);
              }}
            >
              Edit<span className="sr-only"> {row.label.toLowerCase()}</span>
            </Button>
          </dd>
        </div>
      ))}
    </dl>
  );
}
