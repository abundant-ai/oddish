import { auth } from "@clerk/nextjs/server";
import { redirect } from "next/navigation";
import { withOrgSlug } from "./org-path";

/** Server redirect that keeps the caller on the active org's slugged URL. */
export async function orgRedirect(path: string): Promise<never> {
  const { orgSlug } = await auth();
  redirect(withOrgSlug(path, orgSlug));
}
