// Runs before every test file: adds DOM matchers such as toBeInTheDocument(), and small
// stand-ins for browser features jsdom (the simulated browser) does not have.
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// <dialog>.showModal() and .close(): jsdom has the element but not these methods. Opening
// sets the `open` attribute; closing removes it and fires "close", as a browser does.
if (typeof HTMLDialogElement.prototype.showModal !== "function") {
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
    this.removeAttribute("open");
    this.dispatchEvent(new Event("close"));
  };
}

// Radix's menu measures its trigger with ResizeObserver; nothing needs resizing in tests.
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  };
}

afterEach(() => {
  cleanup();
  sessionStorage.clear();
});
