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
        <main className="px-4 py-4 max-w-screen-2xl mx-auto w-full">
          {children}
        </main>
      </SignedIn>
    </>
  );
}
