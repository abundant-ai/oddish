import type { Metadata } from "next";
import localFont from "next/font/local";
import "./globals.css";
import { ClerkProvider } from "@clerk/nextjs";
import { Providers } from "./providers";
import { Footer } from "@/components/footer";
import { PreviewBanner } from "@/components/preview-banner";
import { SpeedInsights } from "@vercel/speed-insights/next";
import { Analytics } from "@vercel/analytics/next";

const geistSans = localFont({
  src: "./fonts/geist-latin.woff2",
  weight: "400 700",
  variable: "--font-geist-sans",
  display: "swap",
});

const geistMono = localFont({
  src: "./fonts/geist-mono-latin.woff2",
  weight: "400 600",
  variable: "--font-geist-mono",
  display: "swap",
});

const fraunces = localFont({
  src: [
    {
      path: "./fonts/fraunces-latin.woff2",
      weight: "400 600",
      style: "normal",
    },
    {
      path: "./fonts/fraunces-italic-latin.woff2",
      weight: "400 600",
      style: "italic",
    },
  ],
  variable: "--font-fraunces",
  display: "swap",
});

const SITE_URL = process.env.NEXT_PUBLIC_APP_URL || "https://www.oddish.app";
const SITE_NAME = "Oddish";
const SITE_TITLE = "Oddish - Eval Scheduler";
const SITE_DESCRIPTION = "Postgres-backed eval scheduler for Harbor tasks";
const SITE_OG_IMAGE = "/oddish.png";

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: SITE_TITLE,
  description: SITE_DESCRIPTION,
  icons: {
    icon: "/oddish.png",
    shortcut: "/oddish.png",
    apple: "/oddish.png",
  },
  openGraph: {
    type: "website",
    siteName: SITE_NAME,
    title: SITE_TITLE,
    description: SITE_DESCRIPTION,
    url: SITE_URL,
    images: [
      {
        url: SITE_OG_IMAGE,
        alt: SITE_NAME,
      },
    ],
  },
  twitter: {
    card: "summary",
    title: SITE_TITLE,
    description: SITE_DESCRIPTION,
    images: [SITE_OG_IMAGE],
  },
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
      signInFallbackRedirectUrl={toAbsoluteUrl(afterSignInUrl)}
      signUpFallbackRedirectUrl={toAbsoluteUrl(afterSignUpUrl)}
    >
      <html
        lang="en"
        className={`${geistSans.variable} ${geistMono.variable} ${fraunces.variable}`}
        data-preview={
          process.env.NEXT_PUBLIC_ODDISH_PREVIEW === "true" ? "true" : undefined
        }
      >
        <body className="bg-background text-foreground flex min-h-screen flex-col font-sans antialiased">
          <Providers>
            <PreviewBanner />
            <div className="flex flex-1 flex-col">{children}</div>
            <Footer />
          </Providers>
          <SpeedInsights />
          <Analytics />
        </body>
      </html>
    </ClerkProvider>
  );
}
