import { orgRedirect } from "@/lib/org-redirect";

export default async function DocumentsRedirect() {
  await orgRedirect("/qa/documents");
}
