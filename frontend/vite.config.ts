import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The same Content-Security-Policy nginx sends (nginx.conf.template), for `vite preview`:
// the production build is then previewed, and screenshotted, under the real policy.
// (The dev server does not send it: Vite injects styles with <style> tags while developing.)
const CSP = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self'",
  "img-src 'self' data:",
  "connect-src 'self' http://localhost:8000",
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "object-src 'none'",
].join("; ");

// Vite builds and serves the app; Vitest reuses this config for the tests.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5173, strictPort: true },
  preview: { port: 4173, strictPort: true, headers: { "Content-Security-Policy": CSP } },
  test: {
    environment: "jsdom", // a simulated browser (DOM, fetch types) for component tests
    setupFiles: ["./src/test/setup.ts"],
    restoreMocks: true,
  },
});
