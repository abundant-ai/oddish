import { NextResponse } from "next/server";
import { auth, currentUser } from "@clerk/nextjs/server";
import { Liveblocks } from "@liveblocks/node";

/**
 * Mints a Liveblocks access token for QA comments. The token only allows
 * rooms under the caller's active Clerk org (`qa:{orgId}:*`), which is the
 * entire org-separation story — the client never gets to pick another
 * org's rooms.
 */
export async function POST() {
  // Before any config check, so the diagnostics below only ever describe
  // this deployment to someone already signed in. (Middleware protects
  // this route too — this is the second lock, not the first.)
  const { userId, orgId } = await auth();
  if (!userId || !orgId) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  // 403, not 503, for both misconfigurations below. Neither fixes itself,
  // and the Liveblocks client only gives up on the status codes it treats
  // as final (400, 401, 403, 404, ...) — a 503 reads as "try again", so a
  // deployment that deliberately leaves the key unset would have every
  // signed-in page retry the mint on a backoff instead of quietly going
  // without comments.
  const secret = process.env.LIVEBLOCKS_SECRET_KEY;
  if (!secret) {
    return NextResponse.json(
      { error: "Liveblocks is not configured (LIVEBLOCKS_SECRET_KEY unset)" },
      { status: 403 }
    );
  }
  // The classic misconfiguration: the *public* key pasted into the secret
  // slot. The SDK throws deep inside authorize() on a malformed key, so
  // catch it here with a message that says what to fix.
  if (!secret.startsWith("sk_")) {
    console.error(
      "[liveblocks-auth] LIVEBLOCKS_SECRET_KEY does not look like a secret key"
    );
    return NextResponse.json(
      {
        error:
          "LIVEBLOCKS_SECRET_KEY must be the secret key (sk_...), not the public key",
      },
      { status: 403 }
    );
  }

  try {
    const user = await currentUser();
    const liveblocks = new Liveblocks({ secret });
    const session = liveblocks.prepareSession(userId, {
      userInfo: {
        name: user?.fullName || user?.username || "Unknown",
        avatar: user?.imageUrl,
      },
    });
    session.allow(`qa:${orgId}:*`, session.FULL_ACCESS);

    const { status, body } = await session.authorize();
    if (status >= 400) {
      // Surfaces e.g. a revoked or wrong-project key as Liveblocks reports it.
      console.error(`[liveblocks-auth] authorize failed (${status}): ${body}`);
    }
    return new NextResponse(body, { status });
  } catch (error) {
    console.error("[liveblocks-auth] token mint failed:", error);
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Auth failed" },
      { status: 500 }
    );
  }
}
