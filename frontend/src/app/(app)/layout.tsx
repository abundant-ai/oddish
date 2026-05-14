import { RedirectToSignIn, Show } from "@clerk/nextjs";
import { Nav } from "@/components/nav";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <Show when="signedOut">
        <RedirectToSignIn />
      </Show>
      <Show when="signedIn">
        <Nav />
        <main className="mx-auto w-full max-w-(--breakpoint-2xl) px-4 py-4">
          {children}
        </main>
      </Show>
    </>
  );
}
