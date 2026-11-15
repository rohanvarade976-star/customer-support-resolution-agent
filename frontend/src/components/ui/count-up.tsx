// A number that counts up to its value when it appears (and from its old value when it
// changes). With prefers-reduced-motion it just shows the value. Display only: the number is
// exactly the one it is given.

import { useEffect, useRef, useState } from "react";

import { usePrefersReducedMotion } from "../../hooks/usePrefersReducedMotion";

const STEPS = 18;
const STEP_MS = 35; // about 630 ms in all

export function CountUp({ value, className }: { value: number; className?: string }) {
  const reduced = usePrefersReducedMotion();
  const [shown, setShown] = useState(0);
  const from = useRef(0);

  useEffect(() => {
    if (reduced) return;
    const start = from.current;
    let step = 0;
    const timer = setInterval(() => {
      step += 1;
      const progress = 1 - (1 - step / STEPS) ** 3; // ease out
      const next = step >= STEPS ? value : Math.round(start + (value - start) * progress);
      from.current = next;
      setShown(next);
      if (step >= STEPS) clearInterval(timer);
    }, STEP_MS);
    return () => {
      clearInterval(timer);
    };
  }, [value, reduced]);

  return <span className={className}>{reduced ? value : shown}</span>;
}
