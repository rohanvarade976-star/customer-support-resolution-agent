// The run page's headline card: an icon that shows the state (pulsing while the assistant works,
// a gentle pop when a state is reached), a plain sentence, and the four-stage stepper. The
// wording is fixed per status; nothing here decides anything (ADR-023).

import { CircleAlert, CircleCheck, Clock, Headset, MessageCircleQuestionMark, Sparkles, type LucideIcon } from "lucide-react";

import type { RunStatus, RunStatusEvent } from "../../api/types";
import { cn } from "../../lib/utils";
import { RunProgress } from "../RunProgress";
import { Card, CardContent } from "../ui/card";

type Tone = "working" | "success" | "attention" | "info" | "danger";

interface Hero {
  title: string;
  text: string;
  tone: Tone;
  icon: LucideIcon;
}

const HERO: Record<RunStatus, Hero> = {
  created: { title: "We've received your request", text: "Our assistant is about to start on it.", tone: "working", icon: Sparkles },
  investigating: {
    title: "Our assistant is on it",
    text: "It's checking your order and our policy. This usually takes under a minute.",
    tone: "working",
    icon: Sparkles,
  },
  awaiting_customer: {
    title: "We need a quick answer from you",
    text: "Please answer the question below so we can continue.",
    tone: "attention",
    icon: MessageCircleQuestionMark,
  },
  awaiting_approval: {
    title: "Waiting for a team member's approval",
    text: "Your refund request has been checked against our rules and now needs a person to approve it.",
    tone: "attention",
    icon: Clock,
  },
  executing: {
    title: "Processing your refund",
    text: "The payment service is working on it.",
    tone: "working",
    icon: Sparkles,
  },
  completed: { title: "All done", text: "Your request is resolved. The details are below.", tone: "success", icon: CircleCheck },
  escalated: {
    title: "With a specialist",
    text: "A support specialist has your case and will reply here.",
    tone: "info",
    icon: Headset,
  },
  failed: {
    title: "Something went wrong on our side",
    text: "Your request is saved and a person will follow up.",
    tone: "danger",
    icon: CircleAlert,
  },
};

const SPECIALIST_RESOLVED: Hero = {
  title: "Resolved by a specialist",
  text: "A support specialist answered and closed this request.",
  tone: "success",
  icon: CircleCheck,
};

const TONE_CARD: Record<Tone, string> = {
  working: "border-primary-200 from-primary-50",
  success: "border-success-200 from-success-50",
  attention: "border-warning-200 from-warning-50",
  info: "border-info-200 from-info-50",
  danger: "border-danger-200 from-danger-50",
};

const TONE_ICON: Record<Tone, string> = {
  working: "bg-primary-600 text-white animate-pulse-ring",
  success: "bg-success-600 text-white",
  attention: "bg-warning-500 text-white",
  info: "bg-info-600 text-white",
  danger: "bg-danger-600 text-white",
};

export function RunHero({ status, resolvedBySpecialist }: { status: RunStatusEvent; resolvedBySpecialist: boolean }) {
  const hero = resolvedBySpecialist ? SPECIALIST_RESOLVED : HERO[status.status];
  const Icon = hero.icon;
  return (
    <Card className={cn("overflow-hidden bg-linear-to-br to-white", TONE_CARD[hero.tone])} data-testid="run-hero" data-tone={hero.tone}>
      <CardContent className="space-y-6 p-6">
        <div className="flex items-center gap-4">
          {/* Keyed by the status, so reaching a new state replays the pop. */}
          <span
            key={`${status.status}-${resolvedBySpecialist ? "r" : ""}`}
            className={cn("grid size-12 shrink-0 place-items-center rounded-full", hero.tone !== "working" && "animate-pop", TONE_ICON[hero.tone])}
          >
            <Icon className="size-6" aria-hidden="true" />
          </span>
          <div className="min-w-0">
            <h2 className="text-xl font-semibold tracking-tight text-gray-900">{hero.title}</h2>
            <p className="mt-0.5 text-sm text-gray-700">{hero.text}</p>
          </div>
        </div>
        <RunProgress status={status.status} />
      </CardContent>
    </Card>
  );
}
