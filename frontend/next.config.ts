import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  allowedDevOrigins: ["local.oddish.app"],
  output: "standalone",
  // Expose Vercel system env vars to the browser bundle so the
  // Logfire browser SDK can tag spans with the PR / commit / branch
  // they came from. Without this, only the backend + Next.js edge
  // spans carry `oddish.pr` and the browser side looks anonymous
  // across all PR previews.
  env: {
    NEXT_PUBLIC_VERCEL_GIT_PULL_REQUEST_ID:
      process.env.VERCEL_GIT_PULL_REQUEST_ID || "",
    NEXT_PUBLIC_VERCEL_GIT_COMMIT_SHA:
      process.env.VERCEL_GIT_COMMIT_SHA || "",
    NEXT_PUBLIC_VERCEL_GIT_COMMIT_REF:
      process.env.VERCEL_GIT_COMMIT_REF || "",
    NEXT_PUBLIC_VERCEL_ENV: process.env.VERCEL_ENV || "",
  },
};

export default nextConfig;
