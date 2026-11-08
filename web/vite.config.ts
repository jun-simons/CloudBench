import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the gateway runs on :8000; proxy API and stream calls to it.
const gateway = process.env.CLOUDBENCH_GATEWAY_URL ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    proxy: {
      "/v1": { target: gateway, changeOrigin: true },
    },
  },
});
