import { NextRequest, NextResponse } from "next/server";
import { auth, clerkClient } from "@clerk/nextjs/server";

/**
 * User resolution for Liveblocks comments, backed by Clerk.
 *
 * - `?ids=a,b,c` → display info for those user ids, in order (resolveUsers)
 * - `?query=al` → org member ids matching the text (mention suggestions);
 *   suggestions are scoped to the caller's active org, so cross-org
 *   mentions are impossible.
 */
export async function GET(request: NextRequest) {
  const { userId, orgId } = await auth();
  if (!userId || !orgId) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const client = await clerkClient();
  const ids = request.nextUrl.searchParams.get("ids");

  if (ids !== null) {
    const userIds = ids.split(",").filter(Boolean);
    const { data } = await client.users.getUserList({
      userId: userIds,
      limit: userIds.length,
    });
    const byId = new Map(data.map((u) => [u.id, u]));
    return NextResponse.json(
      userIds.map((id) => {
        const user = byId.get(id);
        return user
          ? {
              name: user.fullName || user.username || "Unknown",
              avatar: user.imageUrl,
            }
          : undefined;
      }),
    );
  }

  const query = (request.nextUrl.searchParams.get("query") ?? "").toLowerCase();
  const { data: memberships } =
    await client.organizations.getOrganizationMembershipList({
      organizationId: orgId,
      limit: 100,
    });
  const matches = memberships
    .filter((m) => {
      const name =
        `${m.publicUserData?.firstName ?? ""} ${m.publicUserData?.lastName ?? ""}`.toLowerCase();
      const email = (m.publicUserData?.identifier ?? "").toLowerCase();
      return !query || name.includes(query) || email.includes(query);
    })
    .map((m) => m.publicUserData?.userId)
    .filter(Boolean)
    .slice(0, 10);
  return NextResponse.json(matches);
}
