import type { Metadata } from "next";
import { Fraunces, Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import { ClerkProvider } from "@clerk/nextjs";
import { Providers } from "./providers";
import { Footer } from "@/components/footer";

const geistSans = Geist({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-geist-sans",
  display: "swap",
});

const geistMono = Geist_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-geist-mono",
  display: "swap",
});

const fraunces = Fraunces({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  style: ["normal", "italic"],
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
      >
        <body className="flex min-h-screen flex-col bg-background font-sans text-foreground antialiased">
          <Providers>
            <div className="flex flex-1 flex-col">{children}</div>
            <Footer />
          </Providers>
        </body>
      </html>
    </ClerkProvider>
  );
}
