import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  allowedDevOrigins: ["local.oddish.app"],
  output: "standalone",
  // Serve forward navigations to recently visited dynamic pages from the
  // client router cache instead of a fresh server render. Back/forward
  // already behaves this way; this extends it to Link/router.push
  // navigations. The segment cache floors the value at 30s.
  experimental: {
    staleTimes: {
      dynamic: 30,
    },
  },
  // Expose Vercel git/env vars to the browser bundle so Logfire spans
  // emitted from the browser carry the same PR / commit / branch tags
  // as backend and edge spans.
  env: {
    NEXT_PUBLIC_VERCEL_GIT_PULL_REQUEST_ID:
      process.env.VERCEL_GIT_PULL_REQUEST_ID || "",
    NEXT_PUBLIC_VERCEL_GIT_COMMIT_SHA: process.env.VERCEL_GIT_COMMIT_SHA || "",
    NEXT_PUBLIC_VERCEL_GIT_COMMIT_REF: process.env.VERCEL_GIT_COMMIT_REF || "",
    NEXT_PUBLIC_VERCEL_ENV: process.env.VERCEL_ENV || "",
  },
};

export default nextConfig;
