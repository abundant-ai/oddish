import type { Metadata } from "next";
import "./globals.css";
import { ClerkProvider } from "@clerk/nextjs";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "Oddish - Eval Scheduler",
  description: "Postgres-backed eval scheduler for Harbor tasks",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const appUrl = process.env.NEXT_PUBLIC_APP_URL;
  const signInUrl = process.env.NEXT_PUBLIC_CLERK_SIGN_IN_URL;
  const signUpUrl = process.env.NEXT_PUBLIC_CLERK_SIGN_UP_URL;
  const afterSignInUrl =
    process.env.NEXT_PUBLIC_CLERK_AFTER_SIGN_IN_URL || "/dashboard";
  const afterSignUpUrl =
    process.env.NEXT_PUBLIC_CLERK_AFTER_SIGN_UP_URL || "/dashboard";

  const toAbsoluteUrl = (value?: string) => {
    if (!value) {
      return undefined;
    }
    if (value.startsWith("http://") || value.startsWith("https://")) {
      return value;
    }
    if (!appUrl) {
      return value;
    }
    const normalized = value.startsWith("/") ? value : `/${value}`;
    return `${appUrl}${normalized}`;
  };

  return (
    <ClerkProvider
      signInUrl={toAbsoluteUrl(signInUrl)}
      signUpUrl={toAbsoluteUrl(signUpUrl)}
      afterSignInUrl={toAbsoluteUrl(afterSignInUrl)}
      afterSignUpUrl={toAbsoluteUrl(afterSignUpUrl)}
    >
      <html lang="en">
        <body className="antialiased min-h-screen">
          <Providers>{children}</Providers>
        </body>
      </html>
    </ClerkProvider>
  );
}
