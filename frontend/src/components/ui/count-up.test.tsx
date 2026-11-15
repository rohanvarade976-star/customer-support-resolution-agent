import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CountUp } from "./count-up";

// jsdom has no matchMedia, so these tests install one that answers for "reduce".
function setReducedMotion(reduce: boolean): void {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: reduce && query.includes("reduce"),
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }));
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

// The stylesheet side of reduced motion (animations and transitions switched off) is checked in a
// real browser by `npm run screenshots`, which jsdom cannot do.
describe("CountUp and reduced motion", () => {
  it("shows the final value at once when the person prefers reduced motion", () => {
    setReducedMotion(true);
    render(<CountUp value={7} />);
    expect(screen.getByText("7")).toBeInTheDocument();
  });

  it("counts up to the exact value when motion is allowed", () => {
    vi.useFakeTimers();
    setReducedMotion(false);
    render(<CountUp value={7} />);
    expect(screen.getByText("0")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(screen.getByText("7")).toBeInTheDocument();
  });

  it("does not animate when the browser cannot say (no matchMedia)", () => {
    render(<CountUp value={3} />);
    expect(screen.getByText("3")).toBeInTheDocument();
  });
});
