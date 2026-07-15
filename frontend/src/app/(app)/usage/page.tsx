import { redirect } from "next/navigation";

// Usage moved into the admin dashboard; keep old bookmarks/links working.
export default function UsagePage() {
  redirect("/admin?tab=usage");
}
