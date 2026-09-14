import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  allowedDevOrigins: ["local.oddish.app"],
  output: "standalone",
  experimental: {
    staleTimes: {
      dynamic: 30,
    },
  },
  env: {
    // Opt this combined branch's preview into direct calls; other deployments
    // retain the explicit flag/default. Template names are public, not secrets.
    NEXT_PUBLIC_API_DIRECT:
      process.env.NEXT_PUBLIC_API_DIRECT ??
      (process.env.VERCEL_ENV === "preview" &&
      process.env.VERCEL_GIT_COMMIT_REF === "perf/request-path-combined"
        ? "1"
        : "0"),
    NEXT_PUBLIC_CLERK_JWT_TEMPLATE:
      process.env.NEXT_PUBLIC_CLERK_JWT_TEMPLATE ||
      process.env.CLERK_JWT_TEMPLATE ||
      "",
    NEXT_PUBLIC_VERCEL_GIT_PULL_REQUEST_ID:
      process.env.VERCEL_GIT_PULL_REQUEST_ID || "",
    NEXT_PUBLIC_VERCEL_GIT_COMMIT_SHA: process.env.VERCEL_GIT_COMMIT_SHA || "",
    NEXT_PUBLIC_VERCEL_GIT_COMMIT_REF: process.env.VERCEL_GIT_COMMIT_REF || "",
    NEXT_PUBLIC_VERCEL_ENV: process.env.VERCEL_ENV || "",
  },
};

export default nextConfig;
