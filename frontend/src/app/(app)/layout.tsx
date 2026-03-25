import { SignedIn, SignedOut, RedirectToSignIn } from "@clerk/nextjs";
import { Nav } from "@/components/nav";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <SignedOut>
        <RedirectToSignIn />
      </SignedOut>
      <SignedIn>
        <Nav />
        <main className="mx-auto w-full max-w-screen-2xl px-4 py-4">
          {children}
        </main>
      </SignedIn>
    </>
  );
}
