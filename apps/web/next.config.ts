import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The workspace package ships raw TypeScript (main -> generated/index.ts),
  // so Next must transpile it.
  transpilePackages: ["@chanchito/product-model"],
};

export default nextConfig;
