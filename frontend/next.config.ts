import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  distDir: process.env.COOLDOWN_NEXT_DIST_DIR || ".next",
  cacheComponents: true,
  partialPrefetching: true,
  async redirects() {
    return [{source: "/", destination: process.env.NEXT_PUBLIC_SCENARIO_APP_URL || "http://localhost:5173", permanent: false}];
  },
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
