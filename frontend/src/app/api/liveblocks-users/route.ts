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
type Member = { userId: string; name: string; avatar?: string; email: string };

/**
 * Org membership, briefly cached per server instance. Both callers are
 * chatty — every comment render resolves its authors, and every keystroke
 * after an `@` asks for suggestions — and each miss pages through the whole
 * membership. A minute of staleness costs a new member a short wait before
 * they can be mentioned; without it, a large org pays that walk on every
 * keystroke and Clerk feels the rate limit.
 */
const MEMBER_TTL_MS = 60_000;
const memberCache = new Map<string, { at: number; members: Member[] }>();

async function listMembers(orgId: string, now: number): Promise<Member[]> {
  const hit = memberCache.get(orgId);
  if (hit && now - hit.at < MEMBER_TTL_MS) return hit.members;

  const client = await clerkClient();
  const members: Member[] = [];
  // Page through the full membership — a single limit:100 call would make
  // members beyond the first page unmentionable and unresolvable. The hard
  // ceiling is a runaway guard, not an expected size.
  for (let offset = 0; members.length < 2000; offset += 100) {
    const { data } = await client.organizations.getOrganizationMembershipList({
      organizationId: orgId,
      limit: 100,
      offset,
    });
    for (const m of data) {
      const user = m.publicUserData;
      if (!user?.userId) continue;
      const name = `${user.firstName ?? ""} ${user.lastName ?? ""}`.trim();
      members.push({
        userId: user.userId,
        name: name || user.identifier || "Unknown",
        avatar: user.imageUrl,
        email: user.identifier ?? "",
      });
    }
    if (data.length < 100) break;
  }

  memberCache.set(orgId, { at: now, members });
  return members;
}

export async function GET(request: NextRequest) {
  const { userId, orgId } = await auth();
  if (!userId || !orgId) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  let members: Member[];
  try {
    members = await listMembers(orgId, Date.now());
  } catch (error) {
    console.error("[liveblocks-users] membership lookup failed:", error);
    return NextResponse.json(
      { error: "Failed to list org members" },
      { status: 500 },
    );
  }

  const ids = request.nextUrl.searchParams.get("ids");
  if (ids !== null) {
    const byId = new Map(members.map((m) => [m.userId, m]));
    return NextResponse.json(
      ids.split(",").map((id) => {
        const member = byId.get(id);
        if (!member) return undefined; // not in this org — do not resolve
        return { name: member.name, avatar: member.avatar };
      }),
    );
  }

  const query = (request.nextUrl.searchParams.get("query") ?? "").toLowerCase();
  const matches = members
    .filter(
      (m) =>
        !query ||
        m.name.toLowerCase().includes(query) ||
        m.email.toLowerCase().includes(query),
    )
    .map((m) => m.userId)
    .slice(0, 10);
  return NextResponse.json(matches);
}
