import { RedirectToSignIn, Show } from "@clerk/nextjs";
import { Nav } from "@/components/nav";
import { CommentsProvider } from "@/components/comments/comments-provider";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <Show when="signed-out">
        <RedirectToSignIn />
      </Show>
      <Show when="signed-in">
        <CommentsProvider>
          <Nav />
          <main className="mx-auto w-full max-w-(--breakpoint-2xl) px-3 py-3 sm:px-4 sm:py-4">
            {children}
          </main>
        </CommentsProvider>
      </Show>
    </>
  );
}
