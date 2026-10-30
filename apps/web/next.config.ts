import type { NextConfig } from "next";

// `next dev` refuses its dev resources (hot reload) to origins other than
// localhost and the bound host, so a phone opening the LAN address of a
// DASHBOARD_HOST=0.0.0.0 dev server needs that address listed here
// (comma-separated hostnames; see USAGE.md). Ignored by production builds.
const devOrigins = (process.env.DASHBOARD_DEV_ORIGINS ?? "")
  .split(",")
  .map((origin) => origin.trim())
  .filter(Boolean);

const nextConfig: NextConfig = {
  // apps/web/Dockerfile ships `.next/standalone` (server.js plus only the
  // traced node_modules) and builds with NEXT_STANDALONE=1; a host deploy
  // keeps the plain build that `next start` serves.
  ...(process.env.NEXT_STANDALONE === "1" && { output: "standalone" as const }),
  // The workspace package ships raw TypeScript (main -> generated/index.ts),
  // so Next must transpile it.
  transpilePackages: ["@chanchito/product-model"],
  ...(devOrigins.length > 0 && { allowedDevOrigins: devOrigins }),
};

export default nextConfig;
