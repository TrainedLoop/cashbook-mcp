/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  base: "/admin/",
  plugins: [react()],
  build: {
    outDir: "../src/cashbook_mcp/admin_web/static",
    emptyOutDir: true,
    assetsDir: "assets",
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/setup-tests.ts"],
  },
});
