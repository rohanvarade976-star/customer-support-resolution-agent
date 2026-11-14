import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Vite builds and serves the app; Vitest reuses this config for the tests.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5173, strictPort: true },
  test: {
    environment: "jsdom", // a simulated browser (DOM, fetch types) for component tests
    setupFiles: ["./src/test/setup.ts"],
    restoreMocks: true,
  },
});
