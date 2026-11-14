import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { currentToken } from "../auth/sessionStore";
import { ANITA, fakeApi, json, PRIYA, renderApp } from "../test/helpers";

const TOKEN = { access_token: "jwt-abc", token_type: "bearer", expires_in: 3600 };

async function logIn(email: string, password: string) {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Email"), email);
  await user.type(screen.getByLabelText("Password"), password);
  await user.click(screen.getByRole("button", { name: "Log in" }));
  return user;
}

describe("LoginPage", () => {
  it("logs a customer in, keeps the token for this tab, and opens their orders", async () => {
    const calls = fakeApi({
      "POST /auth/login": () => json(TOKEN),
      "GET /me": () => json(PRIYA),
      "GET /me/orders": () => json([]),
    });
    renderApp("/login");

    await logIn("priya@example.com", "secret");

    expect(await screen.findByRole("heading", { name: "My orders" })).toBeInTheDocument();
    expect(calls[0]).toMatchObject({ method: "POST", path: "/auth/login", body: { email: "priya@example.com", password: "secret" } });
    // The role came from GET /me, called with the new token in the header.
    expect(calls[1]).toMatchObject({ path: "/me", authorization: "Bearer jwt-abc" });
    expect(sessionStorage.getItem("support-agent.session")).toContain("jwt-abc");
  });

  it("sends a reviewer to the approval queue", async () => {
    fakeApi({
      "POST /auth/login": () => json(TOKEN),
      "GET /me": () => json(ANITA),
      "GET /approvals": () => json({ items: [] }),
    });
    renderApp("/login");

    await logIn("anita@example.com", "secret");

    expect(await screen.findByRole("heading", { name: "Approval queue" })).toBeInTheDocument();
  });

  it("shows the API's message on a wrong password and stores nothing", async () => {
    fakeApi({
      "POST /auth/login": () =>
        json({ success: false, error_code: "INVALID_CREDENTIALS", message: "Incorrect email or password." }, 401),
    });
    renderApp("/login");

    await logIn("priya@example.com", "wrong");

    expect(await screen.findByRole("alert")).toHaveTextContent("Incorrect email or password.");
    expect(sessionStorage.getItem("support-agent.session")).toBeNull();
    expect(currentToken()).toBeNull();
  });

  it("logout clears the token from memory and storage", async () => {
    fakeApi({
      "POST /auth/login": () => json(TOKEN),
      "GET /me": () => json(PRIYA),
      "GET /me/orders": () => json([]),
    });
    renderApp("/login");
    const user = await logIn("priya@example.com", "secret");
    await screen.findByRole("heading", { name: "My orders" });

    await user.click(screen.getByRole("button", { name: "Log out" }));

    expect(await screen.findByRole("button", { name: "Log in" })).toBeInTheDocument();
    expect(sessionStorage.getItem("support-agent.session")).toBeNull();
    expect(currentToken()).toBeNull();
  });
});
