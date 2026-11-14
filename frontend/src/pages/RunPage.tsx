// A run's live page (customer owner or reviewer): a progress stepper, the verified reply, a
// reply box while the run waits for the customer, a live-connection indicator, and the
// detailed timeline. Everything shown comes from useRunStream (SSE with polling fallback).

import { ArrowLeft, MessageSquareText, Send, ShieldCheck } from "lucide-react";
import { useState, type SyntheticEvent } from "react";
import { Link, useParams } from "react-router-dom";

import { describeError } from "../api/client";
import type { Run } from "../api/types";
import { isReviewer, useAuth } from "../auth/auth";
import { PageHeader } from "../components/PageHeader";
import { RunProgress } from "../components/RunProgress";
import { RunTimeline } from "../components/RunTimeline";
import { ErrorState } from "../components/states";
import { Alert } from "../components/ui/alert";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { FieldHint, Label, Textarea } from "../components/ui/form";
import { Skeleton } from "../components/ui/skeleton";
import { useToast } from "../components/ui/toast-context";
import { useRunStream, type RunStreamOptions, type StreamMode } from "../hooks/useRunStream";
import { runProgress } from "../lib/runStages";
import { cn } from "../lib/utils";

const MAX_REPLY_LENGTH = 2000; // the API's limit (schemas/messages.py)

const MODE: Record<StreamMode, { text: string; dot: string; title: string }> = {
  connecting: { text: "Connecting…", dot: "bg-gray-400 animate-pulse", title: "Opening a live connection" },
  live: { text: "Live", dot: "bg-success-500 animate-pulse", title: "Updates appear as they happen" },
  polling: { text: "Reconnecting…", dot: "bg-warning-500", title: "The live connection dropped; checking every few seconds" },
  done: { text: "Up to date", dot: "bg-gray-400", title: "This request has finished" },
};

function LiveIndicator({ mode }: { mode: StreamMode }) {
  const { text, dot, title } = MODE[mode];
  return (
    <span
      title={title}
      className="inline-flex items-center gap-2 rounded-full border border-gray-200 bg-white px-3 py-1 text-xs font-medium text-gray-700 shadow-xs"
    >
      <span className={cn("size-2 rounded-full", dot)} aria-hidden="true" />
      <span aria-live="polite">{text}</span>
    </span>
  );
}

const SUMMARY_TONE = {
  default: "text-gray-600",
  attention: "text-warning-800",
  warning: "text-warning-800",
  danger: "text-danger-700",
  success: "text-success-700",
};

export function RunPage({ streamOptions }: { streamOptions?: RunStreamOptions }) {
  const { runId = "" } = useParams();
  const { api, user } = useAuth();
  const reviewer = isReviewer(user);
  const live = useRunStream(api, runId, streamOptions);
  const status = live.status;
  const progress = status === null ? null : runProgress(status.status);

  return (
    <div className="mx-auto max-w-3xl">
      <PageHeader
        title={reviewer ? "Run timeline" : "Your request"}
        description={reviewer ? "Every recorded step of this run." : "Updates appear here as we work on it. You can leave and come back."}
        actions={<LiveIndicator mode={live.mode} />}
      />

      {live.error !== null ? (
        <ErrorState
          message={live.error}
          action={
            <Button asChild variant="outline">
              <Link to={reviewer ? "/approvals" : "/tickets"}>
                <ArrowLeft aria-hidden="true" />
                {reviewer ? "Back to the queue" : "Back to my tickets"}
              </Link>
            </Button>
          }
        />
      ) : status === null || progress === null ? (
        <div className="space-y-6" aria-busy="true" aria-label="Loading">
          <Card className="space-y-5 p-6">
            <Skeleton className="h-6 w-48" />
            <Skeleton className="h-4 w-72" />
            <Skeleton className="h-10 w-full" />
          </Card>
          <Card className="space-y-3 p-6">
            <Skeleton className="h-4 w-32" />
            <Skeleton className="h-3 w-full" />
            <Skeleton className="h-3 w-4/5" />
          </Card>
        </div>
      ) : (
        <div className="space-y-6">
          <Card>
            <CardContent className="space-y-6 p-6">
              <div>
                <h2 className="text-xl font-semibold tracking-tight text-gray-900">{progress.headline}</h2>
                <p className={cn("mt-1 text-sm", SUMMARY_TONE[progress.tone])}>
                  {progress.summary}
                </p>
              </div>
              <RunProgress status={status.status} />
            </CardContent>
          </Card>

          {status.reply !== null && (
            <section
              aria-labelledby="reply-heading"
              className="overflow-hidden rounded-xl border border-primary-200 bg-white shadow-sm"
            >
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-primary-100 bg-primary-50/70 px-6 py-3">
                <h2 id="reply-heading" className="flex items-center gap-2 text-sm font-semibold text-primary-900">
                  <MessageSquareText className="size-4 text-primary-600" aria-hidden="true" />
                  Latest update from Support
                </h2>
                <Badge tone="primary" title="Checked against your records before it was sent">
                  <ShieldCheck aria-hidden="true" />
                  Verified
                </Badge>
              </div>
              {/* Verified by the backend before it was saved; shown as plain text. */}
              <p className="px-6 py-5 text-base leading-relaxed whitespace-pre-line text-gray-900" data-testid="verified-reply">
                {status.reply}
              </p>
            </section>
          )}

          {status.status === "awaiting_customer" && !reviewer && <ReplyBox runId={runId} onSent={live.refresh} />}

          <Card>
            <CardHeader className="px-6 pt-6">
              <CardTitle>Activity</CardTitle>
            </CardHeader>
            <CardContent className="p-6">
              <RunTimeline steps={live.steps} reviewer={reviewer} />
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}

function ReplyBox({ runId, onSent }: { runId: string; onSent: () => Promise<void> }) {
  const { api } = useAuth();
  const toast = useToast();
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: SyntheticEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.request<Run>(`/runs/${runId}/messages`, { method: "POST", body: { text } });
      setText("");
      toast.success("Reply sent", "Thanks. We'll pick it up from here.");
      await onSent();
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="border-warning-200 ring-1 ring-warning-100">
      <form onSubmit={(event) => void submit(event)}>
        <CardContent className="p-6">
          <Label htmlFor="reply">Your reply</Label>
          <Textarea
            id="reply"
            rows={3}
            maxLength={MAX_REPLY_LENGTH}
            placeholder="Type your answer here"
            aria-describedby="reply-hint"
            value={text}
            onChange={(event) => {
              setText(event.target.value);
            }}
          />
          <FieldHint id="reply-hint" className="flex justify-between gap-4">
            <span>Answer the question above. Never share passwords or card numbers.</span>
            <span className="shrink-0 tabular-nums">
              {text.length}/{MAX_REPLY_LENGTH}
            </span>
          </FieldHint>
          {error !== null && (
            <div className="mt-3">
              <Alert tone="danger">{error}</Alert>
            </div>
          )}
          <div className="mt-4 flex justify-end">
            <Button type="submit" loading={busy} disabled={text.trim() === ""}>
              {!busy && <Send aria-hidden="true" />}
              {busy ? "Sending…" : "Send reply"}
            </Button>
          </div>
        </CardContent>
      </form>
    </Card>
  );
}
