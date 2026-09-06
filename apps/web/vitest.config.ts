import { defineConfig } from "vitest/config";
import path from "path";

// The replay logic (lib/monitors/history, /api/wealth) groups snapshots by
// local calendar day, so the suite must run in the zone the app targets.
process.env.TZ = "America/Santiago";

export default defineConfig({
  test: {
    globals: true,
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
});
