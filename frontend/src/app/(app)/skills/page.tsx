import { orgRedirect } from "@/lib/org-redirect";

export default async function SkillsRedirect() {
  await orgRedirect("/qa/skills");
}
