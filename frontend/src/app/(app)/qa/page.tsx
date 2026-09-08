import { orgRedirect } from "@/lib/org-redirect";

export default async function QaPage() {
  await orgRedirect("/qa/runs");
}
