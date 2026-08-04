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
  const secret = process.env.LIVEBLOCKS_SECRET_KEY;
  if (!secret) {
    return NextResponse.json(
      { error: "Liveblocks is not configured" },
      { status: 503 },
    );
  }

  const { userId, orgId } = await auth();
  if (!userId || !orgId) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

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
  return new NextResponse(body, { status });
}
