// What to do or expect next: actions and explanations chosen by code from the run's statuses
// (lib/nextSteps.ts). No AI text decides these (ADR-023).

import { ArrowRight, Clock, Headset, Info, Plus, TriangleAlert, Wallet, type LucideIcon } from "lucide-react";
import { Link } from "react-router-dom";

import type { RunStatusEvent } from "../../api/types";
import { nextSteps, type NextStep } from "../../lib/nextSteps";
import { Button } from "../ui/button";

const ICONS: Record<NextStep["key"], LucideIcon> = {
  specialist: Headset,
  approval: Clock,
  refund: Wallet,
  report: TriangleAlert,
  again: Plus,
  failed: Info,
};

const INFO_TONE = "border-info-200 bg-info-50";

function Step({ step }: { step: NextStep }) {
  const Icon = ICONS[step.key];
  const action = step.kind === "action";
  return (
    <li
      data-testid={step.key === "specialist" ? "specialist-card" : `next-step-${step.key}`}
      className={
        "animate-slide-up flex flex-col gap-4 rounded-xl border px-5 py-4 sm:flex-row sm:items-center " +
        (action ? "border-gray-200 bg-white shadow-sm" : INFO_TONE)
      }
    >
      <div className="flex min-w-0 flex-1 gap-4">
        <span className="grid size-10 shrink-0 place-items-center rounded-full bg-white text-info-700 shadow-xs">
          <Icon className="size-5" aria-hidden="true" />
        </span>
        <div className="min-w-0">
          <h3 className="text-base font-semibold text-gray-900">{step.title}</h3>
          <p className="mt-1 text-sm text-gray-700">{step.text}</p>
        </div>
      </div>
      {action && step.to !== undefined && step.linkLabel !== undefined && (
        <Button asChild variant={step.key === "report" ? "outline" : "primary"} className="shrink-0">
          <Link to={step.to}>
            {step.linkLabel}
            <ArrowRight aria-hidden="true" />
          </Link>
        </Button>
      )}
    </li>
  );
}

export function NextSteps({ status }: { status: RunStatusEvent }) {
  const steps = nextSteps(status);
  if (steps.length === 0) return null;
  return (
    <ul aria-label="Next steps" className="space-y-3" data-testid="next-steps">
      {steps.map((step) => (
        <Step key={step.key} step={step} />
      ))}
    </ul>
  );
}
