import { describe, expect, it } from "vitest";

import { SseParser } from "../api/sse";
import { formatMoney } from "./money";

describe("formatMoney", () => {
  it("formats integer paise without floating point", () => {
    expect(formatMoney(249900, "INR")).toBe("₹2,499.00");
    expect(formatMoney(5, "INR")).toBe("₹0.05");
    expect(formatMoney(123456789, "INR")).toBe("₹12,34,567.89");
    expect(formatMoney(-1050, "INR")).toBe("-₹10.50");
    expect(formatMoney(100, "USD")).toBe("USD 1.00");
  });

  it("never shows a made-up figure for a non-integer", () => {
    expect(formatMoney(10.5, "INR")).toBe("—");
  });
});

describe("SseParser", () => {
  it("joins events split across chunks and ignores comments", () => {
    const parser = new SseParser();
    expect(parser.push('event: status\ndata: {"a"')).toEqual([]);
    expect(parser.push(':1}\n\n: keep-alive\n\nid: e-1\nevent: step\ndata: {"b":2}\n\n')).toEqual([
      { event: "status", data: '{"a":1}', id: null },
      { event: "step", data: '{"b":2}', id: "e-1" },
    ]);
  });

  it("handles CRLF line endings and multi-line data", () => {
    const parser = new SseParser();
    expect(parser.push("data: one\r\ndata: two\r\n\r\n")).toEqual([
      { event: "message", data: "one\ntwo", id: null },
    ]);
  });
});
