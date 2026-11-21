import path from "node:path";
import { defineConfig } from "vitest/config";
import react, { reactCompilerPreset } from "@vitejs/plugin-react";
import babel from "@rolldown/plugin-babel";
import tailwindcss from "@tailwindcss/vite";

// The dev server proxies /api to the backend, so the browser talks to one origin (no CORS).
const API = process.env.LEXAI_API_URL ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react(), babel({ presets: [reactCompilerPreset()] }), tailwindcss()],
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "./src") } },
  server: { proxy: { "/api": { target: API, changeOrigin: true } } },
  preview: { proxy: { "/api": { target: API, changeOrigin: true } } },
  test: { include: ["src/**/*.test.ts"], environment: "node" },
});
