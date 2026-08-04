import { NextRequest, NextResponse } from "next/server";
import { auth, clerkClient } from "@clerk/nextjs/server";

/**
 * User resolution for Liveblocks comments, backed by Clerk and scoped to
 * the caller's active org — both paths read only the org's membership
 * list, so ids outside the org resolve to nothing rather than leaking
 * another user's profile.
 *
 * - `?ids=a,b,c` → display info for those user ids, in order (resolveUsers)
 * - `?query=al` → org member ids matching the text (mention suggestions)
 */
export async function GET(request: NextRequest) {
  const { userId, orgId } = await auth();
  if (!userId || !orgId) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const client = await clerkClient();
  // Page through the full membership — a single limit:100 call would make
  // members beyond the first page unmentionable and unresolvable. The hard
  // ceiling is a runaway guard, not an expected size.
  const memberships = [];
  for (let offset = 0; memberships.length < 2000; offset += 100) {
    const { data } = await client.organizations.getOrganizationMembershipList({
      organizationId: orgId,
      limit: 100,
      offset,
    });
    memberships.push(...data);
    if (data.length < 100) break;
  }

  const ids = request.nextUrl.searchParams.get("ids");
  if (ids !== null) {
    const byId = new Map(
      memberships.map((m) => [m.publicUserData?.userId, m.publicUserData]),
    );
    return NextResponse.json(
      ids.split(",").map((id) => {
        const user = byId.get(id);
        if (!user) return undefined; // not in this org — do not resolve
        const name = `${user.firstName ?? ""} ${user.lastName ?? ""}`.trim();
        return {
          name: name || user.identifier || "Unknown",
          avatar: user.imageUrl,
        };
      }),
    );
  }

  const query = (request.nextUrl.searchParams.get("query") ?? "").toLowerCase();
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
