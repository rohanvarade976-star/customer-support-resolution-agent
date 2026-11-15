// Displays an amount the API computed. Integer arithmetic only, no floats (CLAUDE.md rule 6):
// this formats a number for people, it never calculates one.

const SYMBOLS: Record<string, string> = { INR: "₹" };

export function formatMoney(amountMinor: number, currency: string): string {
  if (!Number.isSafeInteger(amountMinor)) return "—"; // never show a made-up figure
  const sign = amountMinor < 0 ? "-" : "";
  const absolute = Math.abs(amountMinor);
  const major = Math.trunc(absolute / 100); // whole rupees: integer division
  const minor = absolute % 100;
  const majorText = major.toLocaleString("en-IN"); // an integer: grouping only, no rounding
  const symbol = SYMBOLS[currency] ?? `${currency} `;
  return `${sign}${symbol}${majorText}.${String(minor).padStart(2, "0")}`;
}
