// Customers: "What we checked", from the run's fixed source codes (RunStatusEvent.checked,
// ADR-021). Plain words only: no model text, no tool names, no internal codes. An unknown
// code is left out rather than shown raw.

import { CircleCheck, Sparkles } from "lucide-react";

import { sourceText } from "../lib/analysis";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "./ui/card";

export function WhatWeCheckedCard({ checked }: { checked: string[] }) {
  const items = checked.map(sourceText).filter((text): text is string => text !== null);
  if (items.length === 0) return null;
  return (
    <Card data-testid="what-we-checked">
      <CardHeader className="px-6 pt-6">
        <div>
          <CardTitle className="flex items-center gap-2">
            <Sparkles className="size-4 text-primary-600" aria-hidden="true" />
            What we checked
          </CardTitle>
          <CardDescription>Our assistant looked these up in our records before replying.</CardDescription>
        </div>
      </CardHeader>
      <CardContent className="px-6 pb-6">
        <ul className="grid gap-2 sm:grid-cols-2">
          {items.map((text) => (
            <li key={text} className="flex items-center gap-2 text-sm text-gray-800">
              <CircleCheck className="size-4 shrink-0 text-success-600" aria-hidden="true" />
              {text}
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}
