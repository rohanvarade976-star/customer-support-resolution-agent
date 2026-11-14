// Screenshots of every page at phone (375 px) and desktop (1440 px) width, for reviewing the
// layout. Dev-only; run with `npm run screenshots` (it builds first).
//
// - Serves the production build with `vite preview`, which sends the real Content-Security-
//   Policy (vite.config.ts), so a CSP violation shows up here as it would in production.
// - No backend needed: every API call is answered from the fixed fake data below.
// - Fails (exit code 1) on any CSP violation, console error, page error, unknown API call,
//   or a page wider than the screen (horizontal scrolling).
// Images go to screenshots/ (git-ignored).
//
// First time only: `npx playwright install chromium` downloads the browser.

import { mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";
import { preview } from "vite";

const API = "http://localhost:8000";
const OUT = fileURLToPath(new URL("../screenshots/", import.meta.url));
const MIN = 60_000;
const now = Date.now();
const at = (offsetMs) => new Date(now + offsetMs).toISOString();

// ------------------------------------------------------------------ fake data

const PRIYA = { id: "c-1", role: "customer", name: "Priya Sharma", email: "priya@example.com" };
const ANITA = { id: "s-1", role: "reviewer", name: "Anita Desai", email: "anita@example.com" };

const ORDERS = [
  {
    order_number: "ORD-1001",
    status: "delivered",
    currency: "INR",
    paid_amount_minor: 249900,
    placed_at: at(-20 * 1440 * MIN),
    delivered_at: at(-17 * 1440 * MIN),
    items: [{ product_id: "p-1", product_name: "Wireless Noise-Cancelling Headphones", quantity: 1, paid_amount_minor: 249900 }],
    returns: [{ status: "received", requested_at: at(-8 * 1440 * MIN), received_at: at(-5 * 1440 * MIN) }],
  },
  {
    order_number: "ORD-1002",
    status: "shipped",
    currency: "INR",
    paid_amount_minor: 179800,
    placed_at: at(-3 * 1440 * MIN),
    delivered_at: null,
    items: [
      { product_id: "p-2", product_name: "Smart Fitness Band", quantity: 1, paid_amount_minor: 129900 },
      { product_id: "p-3", product_name: "USB-C Charging Cable", quantity: 2, paid_amount_minor: 49900 },
    ],
    returns: [],
  },
  {
    order_number: "ORD-1003",
    status: "delivered",
    currency: "INR",
    paid_amount_minor: 89900,
    placed_at: at(-40 * 1440 * MIN),
    delivered_at: at(-37 * 1440 * MIN),
    items: [{ product_id: "p-4", product_name: "Bluetooth Speaker", quantity: 1, paid_amount_minor: 89900 }],
    returns: [],
  },
];

const TICKETS = {
  limit: 50,
  offset: 0,
  items: [
    {
      id: "t-1",
      order_number: "ORD-1002",
      category: "order_status",
      status: "awaiting_customer",
      summary: "My fitness band order hasn't arrived and the tracking hasn't changed in three days.",
      created_at: at(-25 * MIN),
      latest_run_id: "run-1",
      latest_run_status: "awaiting_customer",
    },
    {
      id: "t-2",
      order_number: "ORD-1001",
      category: "refund_request",
      status: "in_progress",
      summary: "I returned my headphones last week but still haven't got my refund.",
      created_at: at(-3 * 60 * MIN),
      latest_run_id: "run-3",
      latest_run_status: "awaiting_approval",
    },
    {
      id: "t-3",
      order_number: "ORD-1003",
      category: "refund_request",
      status: "resolved",
      summary: "The speaker crackles at high volume. I'd like a refund please.",
      created_at: at(-12 * 1440 * MIN),
      latest_run_id: "run-2",
      latest_run_status: "completed",
    },
    {
      id: "t-4",
      order_number: null,
      category: null,
      status: "escalated",
      summary: "Can I change the delivery address on an order that has already shipped?",
      created_at: at(-2 * 1440 * MIN),
      latest_run_id: "run-4",
      latest_run_status: "escalated",
    },
  ],
};

const MEMORY = [
  { kind: "case_outcome", outcome_code: "refund_succeeded", category: "refund_request", order_number: "ORD-1003", created_at: at(-12 * 1440 * MIN), expires_at: at(78 * 1440 * MIN) },
  { kind: "case_outcome", outcome_code: "status_explained", category: "order_status", order_number: "ORD-0999", created_at: at(-30 * 1440 * MIN), expires_at: at(60 * 1440 * MIN) },
  { kind: "case_outcome", outcome_code: "escalated", category: null, order_number: null, created_at: at(-2 * 1440 * MIN), expires_at: at(88 * 1440 * MIN) },
];

const step = (id, type, minutesAgo, details = {}) => ({ id, type, actor: "system", at: at(-minutesAgo * MIN), details });

const RUN_AWAITING = {
  run: {
    run_id: "run-1",
    ticket_id: "t-1",
    status: "awaiting_customer",
    ticket_status: "awaiting_customer",
    reply:
      "Thanks for reaching out. I can see order ORD-1002 was shipped 3 days ago. To check with the courier, could you confirm the PIN code it is being delivered to?",
    finished: false,
    checked: ["order", "shipment"],
  },
  steps: [
    step("e1", "run.created", 25),
    step("e2", "run.classified", 24),
    step("e3", "investigation.started", 24),
    step("e4", "clarification.requested", 23),
  ],
};

const RUN_COMPLETED = {
  run: {
    run_id: "run-2",
    ticket_id: "t-3",
    status: "completed",
    ticket_status: "resolved",
    reply: "Your refund of ₹899.00 for order ORD-1003 is complete. It should appear in your account within 5–7 business days.",
    finished: true,
    checked: ["order", "return", "payment", "policy"],
  },
  steps: [
    step("f1", "run.created", 900, { ticket_id: "t-3" }),
    step("f2", "run.classified", 899, { category: "refund_request" }),
    step("f3", "investigation.started", 899),
    step("f4", "policy.completed", 898, { citations: ["refund_policy_v2:section_3"] }),
    step("f5", "proposal.created", 898, { proposal_id: "p-2", amount_minor: 89900 }),
    step("f6", "validation.passed", 898),
    step("f7", "approval.requested", 897, { approval_id: "a-2" }),
    step("f8", "approval.approved", 860, { approval_id: "a-2" }),
    step("f9", "refund.submitted", 859, { refund_id: "r-2" }),
    step("f10", "refund.succeeded", 855, { refund_id: "r-2", status: "succeeded" }),
    step("f11", "run.completed", 855),
  ],
};

const APPROVALS = {
  items: [
    { approval_id: "a-1", proposal_id: "p-1", ticket_id: "t-2", order_number: "ORD-1001", customer_name: "Priya Sharma", amount_minor: 249900, currency: "INR", reason: "Return received and inspection passed; no refund issued yet", reasons: [], created_at: at(-50 * MIN), expires_at: at(7 * MIN), run_id: "run-3", ai_action: "refund", risk_flags: ["INJECTION_SUSPECTED", "PROVIDER_FALLBACK"] },
    { approval_id: "a-3", proposal_id: "p-3", ticket_id: "t-7", order_number: "ORD-1042", customer_name: "Arjun Mehta", amount_minor: 54900, currency: "INR", reason: "Item damaged on arrival; photos verified", reasons: [], created_at: at(-35 * MIN), expires_at: at(24 * MIN), run_id: "run-4", ai_action: "refund", risk_flags: [] },
    { approval_id: "a-4", proposal_id: "p-4", ticket_id: "t-8", order_number: "ORD-1077", customer_name: "Meera Iyer", amount_minor: 1299900, currency: "INR", reason: "Wrong item delivered; return received", reasons: [], created_at: at(-5 * MIN), expires_at: at(55 * MIN), run_id: "run-5", ai_action: "refund", risk_flags: ["LOW_CLASSIFIER_CONFIDENCE"] },
  ],
};

const CASE_PENDING = {
  approval_id: "a-1",
  proposal_id: "p-1",
  run_id: "run-3",
  ticket_id: "t-2",
  approval_status: "pending",
  created_at: at(-50 * MIN),
  expires_at: at(7 * MIN),
  decided_at: null,
  customer_name: "Priya Sharma",
  complaint: "I returned my headphones last week but still haven't got my refund. The courier said it was delivered to your warehouse on Monday.",
  ticket_status: "in_progress",
  proposal: {
    order_number: "ORD-1001",
    amount_minor: 249900,
    currency: "INR",
    reason: "Return received and inspection passed; no refund issued yet",
    checks_passed: ["OWNERSHIP_VERIFIED", "RETURN_RECEIVED", "INSPECTION_PASSED", "WITHIN_REFUND_WINDOW", "AMOUNT_WITHIN_BALANCE"],
    payload_hash: "9f2c4e1ab07d3c58e6f1a2b4c9d0e7f3a1b2c3d4e5f60718293a4b5c6d7e8f90",
    status: "pending_approval",
  },
  facts: {
    order_status: "delivered",
    delivered_at: at(-17 * 1440 * MIN),
    return_status: "received",
    return_requested_at: at(-8 * 1440 * MIN),
    return_received_at: at(-5 * 1440 * MIN),
    inspection_status: "passed",
    paid_amount_minor: 249900,
    succeeded_refunds_minor: 0,
    in_progress_refunds_minor: 0,
    refundable_balance_minor: 249900,
    refund_window_days: 30,
    eligible_now: true,
    reasons_now: [],
  },
  governing_policy: { policy_key: "refund_policy", version: "v2", title: "Refund Policy", refund_window_days: 30 },
  citations: [
    { citation: "refund_policy_v2:section_3", policy_title: "Refund Policy", version: "v2", section_title: "Refunds after a return", text: "Once a returned item is received and passes inspection, the amount paid for it is refunded to the original payment method. Refunds are issued within 7 business days of the inspection." },
    { citation: "refund_policy_v2:section_2", policy_title: "Refund Policy", version: "v2", section_title: "Return window", text: "Items can be returned within 30 days of delivery, unused and in their original packaging." },
  ],
  refund: null,
};

const CASE_APPROVED = {
  ...CASE_PENDING,
  approval_id: "a-2",
  approval_status: "approved",
  decided_at: at(-10 * MIN),
  refund: { refund_id: "r-1", status: "succeeded", amount_minor: 249900, provider_reference: "pay_ref_7f3a91c2" },
  facts: { ...CASE_PENDING.facts, succeeded_refunds_minor: 249900, refundable_balance_minor: 0, eligible_now: false, reasons_now: ["ALREADY_REFUNDED"] },
};

// The reviewer's AI analysis (ADR-021): codes and counts, plus the AI's rationale.
const ANALYSIS = {
  run_id: "run-3",
  mode: "multi_agent",
  status: "awaiting_approval",
  recommendation: {
    action: "refund",
    accepted: true,
    reason_codes: [],
    rationale:
      "The order was delivered 17 days ago and the return was received 5 days ago, inside the 30-day window. Inspection passed and no refund has been issued, so the full paid amount is refundable under section 3 of the refund policy.",
    source: "ai_generated",
    verified: false,
  },
  agents: [
    { agent: "classifier", at: at(-50 * MIN), duration_ms: 640, details: { category: "refund", confidence: 0.94 } },
    { agent: "supervisor", at: at(-50 * MIN), duration_ms: 410, details: { specialists: ["investigator", "policy_agent"], added_by_code: [], extra_round: false } },
    { agent: "investigator", at: at(-50 * MIN), duration_ms: 3120, details: { tool_calls: 4, failed_calls: 0, facts: 3, dropped_facts: 0, calls_by_code: 1 } },
    { agent: "policy_agent", at: at(-50 * MIN), duration_ms: 2480, details: { searches: 2, rules: 3, dropped_rules: 1, conflicting_passages: false, no_relevant_policy: false } },
    { agent: "resolution_agent", at: at(-50 * MIN), duration_ms: 2210, details: { action: "refund", attempts: 2, revisions: 1 } },
    { agent: "critic", at: at(-50 * MIN), duration_ms: 980, details: { verdict: "approve", reviews: 2 } },
  ],
  tools: [
    { call_id: "call_1", tool: "get_order", agent: "investigator", outcome: "ok", latency_ms: 14, by_code: false },
    { call_id: "call_2", tool: "get_return_status", agent: "investigator", outcome: "ok", latency_ms: 11, by_code: false },
    { call_id: "call_3", tool: "get_payment_status", agent: "investigator", outcome: "ok", latency_ms: 19, by_code: false },
    { call_id: "required_get_shipment", tool: "get_shipment", agent: "investigator", outcome: "ok", latency_ms: 38, by_code: true },
    { call_id: "search_1", tool: "search_policy", agent: "policy_agent", outcome: "ok", latency_ms: 95, by_code: false },
    { call_id: "search_2", tool: "search_policy", agent: "policy_agent", outcome: "ok", latency_ms: 88, by_code: false },
  ],
  quality: {
    attempts: 2,
    revisions: 1,
    validator_passed: true,
    validation_codes: [],
    critic_verdict: "approve",
    facts_grounded: 3,
    facts_dropped: 0,
    citations_verified: 3,
    citations_dropped: 1,
    reply_replaced: false,
  },
  risk_flags: [
    { code: "INJECTION_SUSPECTED", codes: ["IGNORE_RULES"], count: 1 },
    { code: "PROVIDER_FALLBACK", codes: [], count: 1 },
  ],
  cost: { llm_calls: 9, input_tokens: 14250, output_tokens: 1830, active_ms: 9840, fallback_calls: 1 },
};

const SUMMARY = {
  days: 7,
  since: at(-7 * 1440 * MIN),
  runs: 42,
  resolved_automatically: 27,
  sent_for_approval: 9,
  escalated: 6,
  median_seconds_to_outcome: 8.4,
};

const sse = (snapshot) =>
  [
    `event: status\ndata: ${JSON.stringify(snapshot.run)}\n\n`,
    ...snapshot.steps.map((s) => `id: ${s.id}\nevent: step\ndata: ${JSON.stringify(s)}\n\n`),
    `event: end\ndata: ${JSON.stringify({ reason: "finished" })}\n\n`,
  ].join("");

const BASE_ROUTES = {
  "GET /me/orders": () => ORDERS,
  "GET /tickets?limit=50": () => TICKETS,
  "GET /me/memory": () => MEMORY,
  "GET /runs/run-1/stream": () => ({ sse: sse(RUN_AWAITING) }),
  "GET /runs/run-1/events": () => RUN_AWAITING,
  "GET /runs/run-2/stream": () => ({ sse: sse(RUN_COMPLETED) }),
  "GET /runs/run-2/events": () => RUN_COMPLETED,
  "GET /approvals": () => APPROVALS,
  "GET /approvals/a-1": () => CASE_PENDING,
  "GET /approvals/a-2": () => CASE_APPROVED,
  // Wrapped: the analysis has its own "status" field, which the router would read as HTTP's.
  "GET /runs/run-3/analysis": () => ({ status: 200, body: ANALYSIS }),
  "GET /runs/run-2/analysis": () => ({ status: 200, body: { ...ANALYSIS, run_id: "run-2", status: "completed", risk_flags: [] } }),
  "GET /reviewer/automation-summary": () => SUMMARY,
};

// ------------------------------------------------------------------ pages

const click = (name, role = "button") => async (page) => {
  await page.getByRole(role, { name, exact: true }).first().click();
};

const SCENARIOS = [
  { name: "login", path: "/login", user: null },
  { name: "customer-home", path: "/home", user: PRIYA },
  { name: "customer-home-empty", path: "/home", user: PRIYA, routes: { "GET /tickets?limit=50": () => ({ items: [], limit: 50, offset: 0 }) } },
  { name: "new-request-1-order", path: "/complaints/new?order=ORD-1001", user: PRIYA },
  { name: "new-request-1-error", path: "/complaints/new", user: PRIYA, action: click("Continue") },
  {
    name: "new-request-2-issue",
    path: "/complaints/new?order=ORD-1001",
    user: PRIYA,
    action: async (page) => {
      await click("Continue")(page);
      await page.getByText("Refund not received").click(); // the card, as a person would
    },
  },
  {
    name: "new-request-4-review",
    path: "/complaints/new?order=ORD-1001",
    user: PRIYA,
    action: async (page) => {
      await click("Continue")(page);
      await page.getByText("Refund not received").click(); // the card, as a person would
      await click("Continue")(page);
      await click("Continue")(page);
    },
  },
  { name: "run-awaiting-customer", path: "/runs/run-1", user: PRIYA },
  { name: "run-completed", path: "/runs/run-2", user: PRIYA },
  { name: "tickets", path: "/tickets", user: PRIYA },
  { name: "tickets-error", path: "/tickets", user: PRIYA, expectErrors: true, routes: { "GET /tickets?limit=50": () => ({ status: 503, body: { success: false, error_code: "UNAVAILABLE", message: "The service is temporarily unavailable." } }) } },
  { name: "orders", path: "/orders", user: PRIYA },
  { name: "memory", path: "/memory", user: PRIYA },
  { name: "memory-confirm", path: "/memory", user: PRIYA, action: click("Delete all") },
  { name: "reviewer-queue", path: "/approvals", user: ANITA },
  { name: "reviewer-queue-empty", path: "/approvals", user: ANITA, routes: { "GET /approvals": () => ({ items: [] }) } },
  { name: "reviewer-case", path: "/approvals/a-1", user: ANITA },
  { name: "reviewer-case-confirm", path: "/approvals/a-1", user: ANITA, action: click("Approve refund") },
  { name: "reviewer-case-approved", path: "/approvals/a-2", user: ANITA },
  { name: "reviewer-run", path: "/runs/run-2", user: ANITA },
  { name: "user-menu", path: "/home", user: PRIYA, action: click("Account menu: Priya Sharma") },
  { name: "mobile-menu", path: "/tickets", user: PRIYA, only: "mobile", action: click("Open menu") },
];

const VIEWPORTS = [
  { name: "mobile", width: 375, height: 812 },
  { name: "desktop", width: 1440, height: 900 },
];

// ------------------------------------------------------------------ run

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "Authorization, Content-Type",
  "Access-Control-Allow-Methods": "GET, POST, DELETE",
};

async function shoot(browser, base, viewport, scenario) {
  const problems = [];
  const context = await browser.newContext({ viewport, reducedMotion: "reduce" });
  if (scenario.user !== null) {
    const session = { token: "screenshot-token", user: scenario.user, expiresAt: now + 3_600_000 };
    await context.addInitScript((value) => {
      sessionStorage.setItem("support-agent.session", value);
    }, JSON.stringify(session));
  }
  // Collect CSP violations reported by the page itself.
  await context.addInitScript(() => {
    window.__csp = [];
    document.addEventListener("securitypolicyviolation", (event) => {
      window.__csp.push(`${event.violatedDirective}: ${event.blockedURI || "inline"}`);
    });
  });

  const routes = { ...BASE_ROUTES, ...(scenario.routes ?? {}) };
  await context.route(`${API}/**`, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    const key = `${request.method()} ${url.pathname}${url.search}`;
    const handler = routes[key];
    if (handler === undefined) {
      problems.push(`unknown API call: ${key}`);
      return route.fulfill({ status: 404, headers: CORS, json: { success: false, error_code: "NOT_FOUND", message: "Not found." } });
    }
    const result = handler();
    if (result.sse !== undefined) {
      return route.fulfill({ status: 200, headers: { ...CORS, "Content-Type": "text/event-stream" }, body: result.sse });
    }
    if (result.status !== undefined) return route.fulfill({ status: result.status, headers: CORS, json: result.body });
    return route.fulfill({ status: 200, headers: CORS, json: result });
  });

  const page = await context.newPage();
  page.on("console", (message) => {
    if (message.type() === "error" && !scenario.expectErrors) problems.push(`console: ${message.text()}`);
  });
  page.on("pageerror", (error) => problems.push(`page error: ${error.message}`));

  await page.goto(`${base}${scenario.path.slice(1)}`);
  await page.waitForLoadState("networkidle", { timeout: 5000 }).catch(() => {});
  await page.evaluate(() => document.fonts.ready);
  if (scenario.action !== undefined) {
    await scenario.action(page);
    await page.waitForTimeout(300);
  }
  await page.waitForTimeout(250);

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  if (overflow > 0) problems.push(`page is ${overflow}px wider than the screen`);
  const csp = await page.evaluate(() => window.__csp);
  for (const violation of csp) problems.push(`CSP violation: ${violation}`);

  // A modal or menu is pinned to the screen, so capture the viewport; otherwise the full page.
  const overlay = scenario.action !== undefined && /confirm|menu/.test(scenario.name);
  await page.screenshot({ path: `${OUT}${viewport.name}-${scenario.name}.png`, fullPage: !overlay });
  await context.close();
  return problems;
}

await mkdir(OUT, { recursive: true });
const server = await preview();
const base = server.resolvedUrls?.local[0] ?? "http://localhost:4173/";
const browser = await chromium.launch();
let failures = 0;
try {
  for (const viewport of VIEWPORTS) {
    for (const scenario of SCENARIOS) {
      if (scenario.only !== undefined && scenario.only !== viewport.name) continue;
      const problems = await shoot(browser, base, viewport, scenario);
      const label = `${viewport.name}-${scenario.name}`;
      if (problems.length === 0) console.log(`ok    ${label}`);
      else {
        failures += 1;
        console.log(`FAIL  ${label}\n      ${problems.join("\n      ")}`);
      }
    }
  }
} finally {
  await browser.close();
  await new Promise((resolve) => server.httpServer.close(resolve));
}
console.log(failures === 0 ? `\nAll screenshots saved to ${OUT}` : `\n${failures} page(s) had problems.`);
process.exit(failures === 0 ? 0 : 1);
