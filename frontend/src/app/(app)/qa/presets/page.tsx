import { orgRedirect } from "@/lib/org-redirect";

export default async function PresetsPage() {
  await orgRedirect("/qa/skills");
}
