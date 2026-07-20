"use client";

import { useEffect, useState } from "react";
import useSWR, { mutate } from "swr";
import {
  OrganizationProfile,
  UserProfile,
  useClerk,
  useOrganization,
} from "@clerk/nextjs";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { fetcher } from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  Bell,
  Building2,
  Check,
  Copy,
  Key,
  Plus,
  Trash2,
  User as UserIcon,
  Users,
} from "lucide-react";

/**
 * Clerk appearance tuned for embedded inline use on the settings page.
 *
 * Strategy: let Clerk's internal layout do the work, but strip the chrome
 * (its own card/navbar/header) so the component feels like a native
 * section of the page rather than a modal-inside-a-page. Colors come from
 * the app's theme variables so light/dark mode both look correct.
 */
const clerkEmbeddedAppearance = {
  variables: {
    colorBackground: "hsl(var(--card))",
    colorText: "hsl(var(--foreground))",
    // Use alpha-on-foreground rather than --muted-foreground so the
    // active-device row metadata and other secondary text stay legible
    // in dark mode. (Verified against
    // @clerk/javascript packages/ui/src/customizables/elementDescriptors.ts.)
    colorTextSecondary: "hsl(var(--foreground) / 0.78)",
    colorPrimary: "hsl(var(--primary))",
    colorDanger: "hsl(var(--destructive))",
    colorInputBackground: "hsl(var(--background))",
    colorInputText: "hsl(var(--foreground))",
    colorNeutral: "hsl(var(--foreground))",
    borderRadius: "0.5rem",
    fontFamily: "var(--font-sans)",
    fontSize: "0.875rem",
  },
  elements: {
    rootBox: "w-full",
    cardBox: "w-full max-w-none shadow-none border-0 bg-transparent",
    card: "w-full bg-transparent border-0 shadow-none",
    scrollBox: "w-full gap-0 bg-transparent",
    navbar: "hidden",
    navbarMobileMenuRow: "hidden",
    header: "hidden",
    pageScrollBox: "p-0 border-l-0 bg-transparent",
    page: "gap-0 border-l-0 bg-transparent p-0",
    profilePage: "gap-6",
    organizationProfilePage: "gap-6",
    profileSection: "border-b border-border py-6 first:pt-0 last:border-b-0",
    profileSectionHeader: "mb-2",
    profileSectionTitle: "text-foreground text-sm font-semibold",
    profileSectionTitleText: "text-foreground font-medium",
    profileSectionSubtitle: "text-muted-foreground text-sm",
    profileSectionSubtitleText: "text-muted-foreground",
    profileSectionContent: "gap-3 text-foreground",
    profileSectionItem: "text-foreground",
    profileSectionPrimaryButton:
      "bg-primary text-primary-foreground hover:bg-primary/90 h-8 px-3 text-xs font-medium",
    profileSectionSecondaryButton:
      "border border-border text-foreground hover:bg-muted h-8 px-3 text-xs font-medium",
    activeDevice: "text-foreground",
    activeDeviceListItem: "text-foreground",
    menuButton: "hover:bg-muted rounded-md",
    formButtonPrimary:
      "bg-primary text-primary-foreground hover:bg-primary/90 h-9 text-sm font-medium",
    formButtonReset:
      "text-muted-foreground hover:text-foreground hover:bg-muted h-9 text-sm font-medium",
    formFieldLabel: "text-foreground text-sm font-medium",
    formFieldInput:
      "bg-background border border-input text-foreground focus:ring-2 focus:ring-ring h-9 text-sm",
    formFieldHintText: "text-muted-foreground text-xs",
    formFieldErrorText: "text-destructive text-xs",
    badge: "bg-muted text-muted-foreground border-border",
    dividerLine: "bg-border",
    dividerRow: "my-4",
    avatarBox: "rounded-md border border-border",
    userButtonBox: "flex-row-reverse",
    userPreviewMainIdentifier: "text-foreground text-sm font-medium",
    userPreviewSecondaryIdentifier: "text-muted-foreground text-xs",
    organizationPreviewMainIdentifier: "text-foreground text-sm font-medium",
    organizationPreviewSecondaryIdentifier: "text-muted-foreground text-xs",
  },
};

type SettingsSection =
  | "profile"
  | "workspace"
  | "api-keys"
  | "byok"
  | "notifications";

const SECTIONS: {
  id: SettingsSection;
  label: string;
  description: string;
  icon: typeof UserIcon;
}[] = [
  {
    id: "profile",
    label: "Account",
    description: "Your personal profile, email, and security settings.",
    icon: UserIcon,
  },
  {
    id: "workspace",
    label: "Workspace",
    description: "Invite teammates and manage roles for your workspace.",
    icon: Building2,
  },
  {
    id: "api-keys",
    label: "API keys",
    description: "Programmatic access tokens for the Oddish CLI and API.",
    icon: Key,
  },
  {
    id: "byok",
    label: "Your API key",
    description: "Use your own Anthropic key for your runs.",
    icon: Key,
  },
  {
    id: "notifications",
    label: "Notifications",
    description: "Choose which Slack alerts you receive, and at what cutoffs.",
    icon: Bell,
  },
];

function isSettingsSection(value: string | null): value is SettingsSection {
  return (
    value === "profile" ||
    value === "workspace" ||
    value === "api-keys" ||
    value === "byok" ||
    value === "notifications"
  );
}

interface APIKey {
  id: string;
  name: string;
  key_prefix: string;
  scope: string;
  is_active: boolean;
  expires_at: string | null;
  last_used_at: string | null;
  created_at: string;
}

interface APIKeyPermissions {
  can_create: boolean;
  can_manage: boolean;
  allowed_scopes: string[];
}

const VISIBLE_API_KEY_LIMIT = 8;

function formatDate(dateStr: string | null): string {
  if (!dateStr) return "—";
  return new Date(dateStr).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function formatDateTime(dateStr: string | null): string {
  if (!dateStr) return "Never";
  return new Date(dateStr).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function getScopeLabel(scope: string): string {
  if (scope === "full") return "Full access";
  if (scope === "tasks") return "Tasks";
  if (scope === "read") return "Read only";
  return scope;
}

function ScopeBadge({ scope }: { scope: string }) {
  const variants: Record<string, string> = {
    full: "bg-[color:var(--paper-queued-bg)] text-[color:var(--paper-queued)] border-[color:var(--paper-queued)]/30",
    tasks:
      "bg-[color:var(--paper-running-bg)] text-[color:var(--paper-running)] border-[color:var(--paper-running)]/30",
    read: "bg-[color:var(--paper-pass-bg)] text-[color:var(--paper-pass)] border-[color:var(--paper-pass)]/30",
  };

  return (
    <Badge
      variant="outline"
      className={cn(
        "rounded-md font-mono text-[11px] font-medium tracking-wide uppercase",
        variants[scope] ?? "bg-muted text-muted-foreground"
      )}
    >
      {scope}
    </Badge>
  );
}

// =============================================================================
// Section: reusable layout bits
// =============================================================================

function SectionHeading({
  title,
  description,
}: {
  title: string;
  description?: string;
}) {
  return (
    <div className="space-y-1.5">
      <h2 className="font-display text-2xl font-medium tracking-tight text-[color:var(--paper-ink)]">
        {title}
      </h2>
      {description ? (
        <p className="text-muted-foreground text-sm leading-relaxed">
          {description}
        </p>
      ) : null}
    </div>
  );
}

/**
 * Renders its children but keeps inactive sections in the DOM (mounted)
 * so heavy Clerk components don't re-mount on every section switch.
 * Inactive panels are positioned absolute + opacity-0 so they don't
 * affect layout but stay reachable for ARIA / focus restoration.
 */
function SectionContainer({
  active,
  children,
}: {
  active: boolean;
  children: React.ReactNode;
}) {
  return (
    <div
      role="tabpanel"
      aria-hidden={!active}
      // `inert` keeps inactive panels out of the tab order and disables
      // pointer events without unmounting them — supported as a real
      // boolean prop in React 19 / modern Chromium, Safari, and Firefox.
      inert={!active}
      className={cn(
        "transition-opacity duration-200 ease-out",
        active ? "relative opacity-100" : "absolute inset-0 opacity-0"
      )}
    >
      {children}
    </div>
  );
}

function Panel({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <Card
      className={cn(
        "border-border/80 bg-card/95 rounded-xl shadow-xs",
        className
      )}
    >
      <CardContent className="p-5">{children}</CardContent>
    </Card>
  );
}

function PanelHeader({
  title,
  description,
  action,
  icon: Icon,
}: {
  title: string;
  description?: string;
  action?: React.ReactNode;
  icon?: typeof UserIcon;
}) {
  return (
    <div className="border-border/70 flex items-start justify-between gap-4 border-b pb-4">
      <div className="flex items-start gap-3">
        {Icon ? (
          <div className="border-border bg-muted/60 text-muted-foreground mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md border">
            <Icon className="h-4 w-4" />
          </div>
        ) : null}
        <div className="space-y-1">
          <h3 className="text-foreground text-base leading-none font-semibold tracking-tight">
            {title}
          </h3>
          {description ? (
            <p className="text-muted-foreground text-sm">{description}</p>
          ) : null}
        </div>
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  );
}

// =============================================================================
// API Keys
// =============================================================================

function CreateAPIKeyModal({
  isOpen,
  onClose,
  onKeyCreated,
  allowedScopes,
}: {
  isOpen: boolean;
  onClose: () => void;
  onKeyCreated: (key: string) => void;
  allowedScopes: string[];
}) {
  const [name, setName] = useState("");
  const defaultScope = allowedScopes[0] ?? "tasks";
  const [scope, setScope] = useState(defaultScope);
  const [expiresInDays, setExpiresInDays] = useState("never");
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (isOpen && allowedScopes.length > 0) {
      setScope(defaultScope);
    }
  }, [allowedScopes.length, defaultScope, isOpen]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsLoading(true);
    setError(null);

    try {
      const res = await fetch(`/api/settings/api-keys`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          scope,
          expires_in_days:
            expiresInDays === "never" ? null : Number(expiresInDays),
        }),
      });

      if (!res.ok) {
        const data = await res.json();
        throw new Error(data.error || "Failed to create API key");
      }

      const data = await res.json();
      onKeyCreated(data.key);
      mutate(`/api/settings/api-keys`);
      setName("");
      setScope(defaultScope);
      setExpiresInDays("never");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create API key");
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Create API key</DialogTitle>
          <DialogDescription>
            API keys authenticate the Oddish CLI and backend requests.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="api-key-name">Name</Label>
            <Input
              id="api-key-name"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. CI runner, laptop"
              required
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="api-key-scope">Scope</Label>
            <Select value={scope} onValueChange={setScope}>
              <SelectTrigger id="api-key-scope">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {allowedScopes.includes("full") ? (
                  <SelectItem value="full">Full — all operations</SelectItem>
                ) : null}
                {allowedScopes.includes("tasks") ? (
                  <SelectItem value="tasks">
                    Tasks — run trials, create tasks, read files
                  </SelectItem>
                ) : null}
                {allowedScopes.includes("read") ? (
                  <SelectItem value="read">Read — read-only</SelectItem>
                ) : null}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="api-key-expiration">Expiration</Label>
            <Select value={expiresInDays} onValueChange={setExpiresInDays}>
              <SelectTrigger id="api-key-expiration">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="never">Never expires</SelectItem>
                <SelectItem value="7">7 days</SelectItem>
                <SelectItem value="30">30 days</SelectItem>
                <SelectItem value="90">90 days</SelectItem>
                <SelectItem value="365">1 year</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={isLoading || !name}>
              {isLoading ? "Creating…" : "Create key"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function NewKeyDisplay({
  apiKey,
  onClose,
}: {
  apiKey: string;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    await navigator.clipboard.writeText(apiKey);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <Dialog open={Boolean(apiKey)} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>API key created</DialogTitle>
          <DialogDescription>
            Copy this key now — you won&apos;t be able to see it again.
          </DialogDescription>
        </DialogHeader>

        <div className="border-border bg-background flex items-center gap-2 rounded-md border p-3 font-mono text-sm">
          <code className="flex-1 break-all">{apiKey}</code>
          <Button variant="ghost" size="sm" onClick={handleCopy}>
            {copied ? (
              <>
                <Check className="mr-1 h-3.5 w-3.5 text-[color:var(--paper-pass)]" />
                Copied
              </>
            ) : (
              <>
                <Copy className="mr-1 h-3.5 w-3.5" />
                Copy
              </>
            )}
          </Button>
        </div>

        <DialogFooter>
          <Button onClick={onClose}>Done</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function APIKeysPanel() {
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [newKey, setNewKey] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<string | null>(null);
  const [revokeTarget, setRevokeTarget] = useState<APIKey | null>(null);
  const [revokeError, setRevokeError] = useState<string | null>(null);
  const [showAllKeys, setShowAllKeys] = useState(false);

  const {
    data: keys,
    error,
    isLoading,
  } = useSWR<APIKey[]>(`/api/settings/api-keys`, fetcher);
  const { data: permissions } = useSWR<APIKeyPermissions>(
    `/api/settings/api-keys/permissions`,
    fetcher
  );
  const canCreateAPIKeys = permissions?.can_create ?? false;
  const canManageAPIKeys = permissions?.can_manage ?? false;
  const allowedScopes = permissions?.allowed_scopes ?? [];
  const createRestriction =
    "You don't have permission to create API keys in this organization.";
  const visibleKeys = showAllKeys
    ? (keys ?? [])
    : (keys ?? []).slice(0, VISIBLE_API_KEY_LIMIT);
  const hiddenKeyCount = Math.max(
    0,
    (keys?.length ?? 0) - VISIBLE_API_KEY_LIMIT
  );
  const activeKeyCount = keys?.filter((key) => key.is_active).length ?? 0;
  const recentlyUsedCount =
    keys?.filter((key) => Boolean(key.last_used_at)).length ?? 0;
  const scopeCounts =
    keys?.reduce<Record<string, number>>((counts, key) => {
      counts[key.scope] = (counts[key.scope] ?? 0) + 1;
      return counts;
    }, {}) ?? {};

  const handleRevoke = async () => {
    if (!revokeTarget) return;

    setRevokeError(null);
    setRevoking(revokeTarget.id);
    try {
      const res = await fetch(`/api/settings/api-keys/${revokeTarget.id}`, {
        method: "DELETE",
      });

      if (!res.ok) {
        throw new Error("Failed to revoke key");
      }

      mutate(`/api/settings/api-keys`);
    } catch {
      setRevokeError("Failed to revoke API key");
    } finally {
      setRevoking(null);
      setRevokeTarget(null);
    }
  };

  return (
    <Panel>
      <PanelHeader
        icon={Key}
        title="API keys"
        description="Used by the CLI and direct API integrations."
        action={
          canCreateAPIKeys ? (
            <Button size="sm" onClick={() => setShowCreateModal(true)}>
              <Plus className="mr-1 h-3.5 w-3.5" />
              New key
            </Button>
          ) : null
        }
      />

      <div className="pt-4">
        {permissions && !canCreateAPIKeys ? (
          <Alert className="mb-4">
            <AlertDescription>{createRestriction}</AlertDescription>
          </Alert>
        ) : null}
        {error ? (
          <Alert variant="destructive">
            <AlertTitle>Failed to load API keys</AlertTitle>
            <AlertDescription>
              Check the API connection and try again.
            </AlertDescription>
          </Alert>
        ) : revokeError ? (
          <Alert variant="destructive">
            <AlertTitle>Failed to revoke API key</AlertTitle>
            <AlertDescription>{revokeError}</AlertDescription>
          </Alert>
        ) : isLoading ? (
          <p className="text-muted-foreground py-6 text-center text-sm">
            Loading…
          </p>
        ) : !keys || keys.length === 0 ? (
          <div className="border-border bg-muted/30 flex flex-col items-center gap-2 rounded-lg border border-dashed py-10 text-center">
            <Key className="text-muted-foreground/60 h-8 w-8" />
            <div className="space-y-0.5">
              <p className="text-foreground text-sm font-medium">
                No API keys yet
              </p>
              <p className="text-muted-foreground text-xs">
                {canCreateAPIKeys
                  ? "Create one to use the Oddish CLI from your laptop or CI."
                  : createRestriction}
              </p>
            </div>
            {canCreateAPIKeys ? (
              <Button
                size="sm"
                variant="outline"
                className="mt-2"
                onClick={() => setShowCreateModal(true)}
              >
                <Plus className="mr-1 h-3.5 w-3.5" />
                Create your first key
              </Button>
            ) : null}
          </div>
        ) : (
          <div className="flex flex-col gap-4">
            <div className="grid gap-3 sm:grid-cols-3">
              <div className="border-border bg-muted/30 rounded-lg border p-3">
                <p className="text-muted-foreground text-xs font-medium">
                  Active keys
                </p>
                <p className="text-foreground mt-1 text-2xl font-semibold">
                  {activeKeyCount}
                </p>
              </div>
              <div className="border-border bg-muted/30 rounded-lg border p-3">
                <p className="text-muted-foreground text-xs font-medium">
                  Used keys
                </p>
                <p className="text-foreground mt-1 text-2xl font-semibold">
                  {recentlyUsedCount}
                </p>
              </div>
              <div className="border-border bg-muted/30 rounded-lg border p-3">
                <p className="text-muted-foreground text-xs font-medium">
                  Scopes
                </p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {Object.entries(scopeCounts).map(([scope, count]) => (
                    <Badge key={scope} variant="secondary" className="text-xs">
                      {getScopeLabel(scope)} · {count}
                    </Badge>
                  ))}
                </div>
              </div>
            </div>

            <div className="flex items-center justify-between gap-3">
              <div>
                <p className="text-foreground text-sm font-medium">
                  Recent keys
                </p>
                <p className="text-muted-foreground text-xs">
                  Showing {visibleKeys.length} of {keys.length}
                </p>
              </div>
              {hiddenKeyCount > 0 ? (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => setShowAllKeys((value) => !value)}
                >
                  {showAllKeys ? "Show fewer" : `Show all ${keys.length}`}
                </Button>
              ) : null}
            </div>

            <div className="flex flex-col gap-2">
              {visibleKeys.map((key) => (
                <div
                  key={key.id}
                  className={cn(
                    "border-border bg-background flex flex-col gap-3 rounded-lg border p-3 sm:flex-row sm:items-center sm:justify-between",
                    !key.is_active && "opacity-50"
                  )}
                >
                  <div className="min-w-0">
                    <div className="flex min-w-0 flex-wrap items-center gap-2">
                      <p className="text-foreground truncate text-sm font-medium">
                        {key.name}
                      </p>
                      <ScopeBadge scope={key.scope} />
                    </div>
                    <p className="text-muted-foreground mt-1 font-mono text-xs">
                      {key.key_prefix}… · Last used{" "}
                      {formatDateTime(key.last_used_at)}
                    </p>
                  </div>
                  <div className="flex items-center justify-between gap-3 sm:justify-end">
                    <div className="text-muted-foreground text-xs sm:text-right">
                      <p>Created {formatDate(key.created_at)}</p>
                      <p>Expires {formatDate(key.expires_at)}</p>
                    </div>
                    {canManageAPIKeys && key.is_active ? (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setRevokeTarget(key)}
                        disabled={revoking === key.id}
                        className="text-muted-foreground hover:text-destructive h-7 w-7 p-0"
                        aria-label={`Revoke ${key.name}`}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    ) : null}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      <CreateAPIKeyModal
        isOpen={showCreateModal && canCreateAPIKeys}
        onClose={() => setShowCreateModal(false)}
        onKeyCreated={(key) => {
          setNewKey(key);
          setShowCreateModal(false);
        }}
        allowedScopes={allowedScopes}
      />

      {newKey && (
        <NewKeyDisplay apiKey={newKey} onClose={() => setNewKey(null)} />
      )}

      <AlertDialog
        open={Boolean(revokeTarget)}
        onOpenChange={(open) => {
          if (!open) setRevokeTarget(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              Revoke{" "}
              <span className="font-mono text-sm">{revokeTarget?.name}</span>?
            </AlertDialogTitle>
            <AlertDialogDescription>
              This action cannot be undone. The key will no longer be usable.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={Boolean(revoking)}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={handleRevoke}
              disabled={Boolean(revoking)}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {revoking ? "Revoking…" : "Revoke key"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Panel>
  );
}

// =============================================================================
// Your API key (BYOK)
// =============================================================================

function ByokPanel() {
  const { data } = useSWR<{
    enabled: boolean;
    key_set: boolean;
    key_hint: string;
  }>("/api/settings/byok", fetcher);
  const [keyInput, setKeyInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function saveKey() {
    const key = keyInput.trim();
    if (!key) return;
    setBusy(true);
    setError(null);
    const res = await fetch("/api/settings/byok/keys/anthropic", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    });
    if (!res.ok) setError("Could not save the key. Try again.");
    else setKeyInput("");
    await mutate("/api/settings/byok");
    setBusy(false);
  }

  async function removeKey() {
    setBusy(true);
    setError(null);
    const res = await fetch("/api/settings/byok/keys/anthropic", {
      method: "DELETE",
    });
    if (!res.ok) setError("Could not remove the key. Try again.");
    await mutate("/api/settings/byok");
    setBusy(false);
  }

  return (
    <Panel>
      <PanelHeader
        icon={Key}
        title="Your Anthropic API key"
        description="When enabled for you, your runs use this key instead of the platform key. Otherwise it sits unused."
      />
      <div className="space-y-4 pt-4">
        {error ? (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        ) : null}

        <div className="flex flex-wrap items-center gap-2">
          {data?.key_set ? (
            <Badge>set ••••{data.key_hint}</Badge>
          ) : (
            <Badge variant="outline">not set</Badge>
          )}
          <Badge variant={data?.enabled ? "default" : "outline"}>
            {data?.enabled ? "enabled for you" : "not enabled"}
          </Badge>
        </div>

        {data && !data.enabled ? (
          <p className="text-muted-foreground text-xs">
            You can save a key now; it takes effect once this is enabled for
            your account.
          </p>
        ) : null}

        <div className="flex items-center gap-2">
          <Input
            id="byok-key-anthropic"
            type="password"
            value={keyInput}
            onChange={(e) => setKeyInput(e.target.value)}
            placeholder="sk-ant-..."
            disabled={busy}
            className="h-9 w-64"
            aria-label="Anthropic API key"
          />
          <Button
            type="button"
            size="sm"
            onClick={saveKey}
            disabled={busy || !keyInput.trim()}
          >
            {data?.key_set ? "Replace" : "Save"}
          </Button>
          {data?.key_set ? (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={removeKey}
              disabled={busy}
              className="text-muted-foreground hover:text-destructive h-7 w-7 p-0"
              aria-label="Remove key"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          ) : null}
        </div>
      </div>
    </Panel>
  );
}

// =============================================================================
// Notifications
// =============================================================================

interface AlertPrefs {
  cost_milestone_enabled: boolean;
  expensive_trial_enabled: boolean;
  experiment_failed_enabled: boolean;
  trial_failed_enabled: boolean;
  qa_failed_enabled: boolean;
  experiment_milestone_usd: number | null;
  trial_ping_usd: number | null;
  inherited_experiment_milestone_usd: number;
  inherited_trial_ping_usd: number;
}

const ALERT_TOGGLES: {
  key: keyof AlertPrefs;
  label: string;
  description: string;
}[] = [
  {
    key: "cost_milestone_enabled",
    label: "Expensive experiment",
    description: "An experiment's spend passes a milestone within 24 hours.",
  },
  {
    key: "expensive_trial_enabled",
    label: "Expensive trial",
    description: "A trial finished within 24 hours costs more than your cutoff.",
  },
  {
    key: "experiment_failed_enabled",
    label: "Experiment failed",
    description: "Most of an experiment's trials failed.",
  },
  {
    key: "trial_failed_enabled",
    label: "Trial failed",
    description: "One of your trials crashed or errored.",
  },
  {
    key: "qa_failed_enabled",
    label: "QA failed",
    description: "A task's QA verdict came back bad.",
  },
];

function NotificationsPanel() {
  const { data, mutate: mutatePrefs } = useSWR<AlertPrefs>(
    "/api/settings/notifications",
    fetcher,
  );
  const [draft, setDraft] = useState<Partial<AlertPrefs> | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!data) {
    return (
      <Panel>
        <PanelHeader icon={Bell} title="Notifications" />
        <p className="text-muted-foreground pt-4 text-sm">Loading…</p>
      </Panel>
    );
  }

  const value: AlertPrefs = { ...data, ...draft };
  const set = <K extends keyof AlertPrefs>(key: K, v: AlertPrefs[K]) => {
    setError(null);
    setDraft((d) => ({ ...d, [key]: v }));
  };

  // Empty cutoff field means "inherit the deploy-time default" — stored as null.
  const cutoffField = (
    key: "experiment_milestone_usd" | "trial_ping_usd",
    inherited: number,
    label: string,
  ) => (
    <div className="space-y-1">
      <Label htmlFor={key} className="text-sm">
        {label}
      </Label>
      <Input
        id={key}
        type="number"
        min={1}
        step={50}
        className="h-9 w-40"
        value={value[key] ?? ""}
        placeholder={`Inherits $${inherited.toLocaleString()}`}
        onChange={(e) =>
          set(key, e.target.value === "" ? null : Number(e.target.value))
        }
      />
    </div>
  );

  async function save() {
    for (const key of ["experiment_milestone_usd", "trial_ping_usd"] as const) {
      const v = value[key];
      if (v !== null && (!Number.isFinite(v) || v <= 0)) {
        setError("Cutoffs must be greater than $0, or left blank to inherit.");
        return;
      }
    }
    setSaving(true);
    setError(null);
    const res = await fetch("/api/settings/notifications", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(value),
    }).catch(() => null);
    setSaving(false);
    if (!res || !res.ok) {
      setError("Could not save notification settings. Try again.");
      return;
    }
    setDraft(null);
    void mutatePrefs();
  }

  return (
    <Panel>
      <PanelHeader icon={Bell} title="Notifications" />
      <div className="space-y-5 pt-4">
        {error ? (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        ) : null}

        <div className="space-y-3">
          {ALERT_TOGGLES.map((t) => (
            <label
              key={t.key}
              htmlFor={t.key}
              className="flex cursor-pointer items-start gap-3"
            >
              <Checkbox
                id={t.key}
                checked={value[t.key] as boolean}
                onCheckedChange={(c) => set(t.key, c === true)}
                className="mt-0.5"
              />
              <span className="space-y-0.5">
                <span className="text-foreground block text-sm font-medium">
                  {t.label}
                </span>
                <span className="text-muted-foreground block text-xs">
                  {t.description}
                </span>
              </span>
            </label>
          ))}
        </div>

        <div className="border-border/70 space-y-3 border-t pt-4">
          <p className="text-muted-foreground text-xs">
            Your own cost cutoffs. Leave blank to inherit the workspace default.
          </p>
          <div className="flex flex-wrap gap-4">
            {cutoffField(
              "experiment_milestone_usd",
              value.inherited_experiment_milestone_usd,
              "Experiment milestone ($)",
            )}
            {cutoffField(
              "trial_ping_usd",
              value.inherited_trial_ping_usd,
              "Expensive-trial cutoff ($)",
            )}
          </div>
        </div>

        <div className="flex items-center gap-3">
          <Button size="sm" disabled={saving || draft === null} onClick={save}>
            {saving ? "Saving…" : "Save"}
          </Button>
        </div>
      </div>
    </Panel>
  );
}

// =============================================================================
// Profile
// =============================================================================

function ProfilePanel() {
  return (
    <div className="space-y-6">
      <Panel>
        <PanelHeader
          icon={UserIcon}
          title="Personal account"
          description="Managed by Clerk — update your name, email, password, and connected accounts."
        />
        <div className="space-y-4 pt-4">
          <UserProfile routing="hash" appearance={clerkEmbeddedAppearance} />
        </div>
      </Panel>
      <DeleteAccountPanel />
    </div>
  );
}

const DELETE_CONFIRM_PHRASE = "DELETE";

function DeleteAccountPanel() {
  const { signOut } = useClerk();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [confirmText, setConfirmText] = useState("");
  const [isDeleting, setIsDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const confirmed = confirmText.trim() === DELETE_CONFIRM_PHRASE;

  const closeDialog = () => {
    if (isDeleting) return;
    setDialogOpen(false);
    setConfirmText("");
    setError(null);
  };

  const handleDelete = async () => {
    if (!confirmed || isDeleting) return;
    setIsDeleting(true);
    setError(null);

    try {
      const res = await fetch(`/api/settings/account`, { method: "DELETE" });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        throw new Error(data?.error || "Failed to delete account");
      }
      // The Clerk user is gone; end the (now orphaned) session and land on
      // the public page. Deleting the user can invalidate the session before
      // signOut runs, so a signOut failure must not strand the user on the
      // page — hard-redirect instead.
      try {
        await signOut({ redirectUrl: "/" });
      } catch {
        window.location.assign("/");
      }
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to delete account"
      );
      setIsDeleting(false);
    }
  };

  return (
    <Panel className="border-destructive/40">
      <PanelHeader
        icon={Trash2}
        title="Delete account"
        description="Permanently delete your account and sign-in credentials. This cannot be undone."
        action={
          <Button
            variant="outline"
            size="sm"
            className="border-destructive/50 text-destructive hover:bg-destructive hover:text-destructive-foreground"
            onClick={() => setDialogOpen(true)}
          >
            <Trash2 className="mr-1 h-3.5 w-3.5" />
            Delete account
          </Button>
        }
      />
      <div className="pt-4">
        <p className="text-muted-foreground text-sm leading-relaxed">
          Deleting your account removes your sign-in (Clerk) account and
          deactivates your Oddish user in every workspace. Your workspaces and
          their data are not deleted.
        </p>
      </div>

      <Dialog open={dialogOpen} onOpenChange={(open) => !open && closeDialog()}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Delete your account?</DialogTitle>
            <DialogDescription>
              This permanently deletes your sign-in account and cannot be
              undone. Type{" "}
              <span className="text-foreground font-mono font-semibold">
                {DELETE_CONFIRM_PHRASE}
              </span>{" "}
              to confirm.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-1.5">
            <Label htmlFor="delete-account-confirm">Confirmation</Label>
            <Input
              id="delete-account-confirm"
              value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)}
              placeholder={DELETE_CONFIRM_PHRASE}
              autoComplete="off"
              disabled={isDeleting}
            />
          </div>

          {error ? (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          ) : null}

          <DialogFooter>
            <Button
              type="button"
              variant="ghost"
              onClick={closeDialog}
              disabled={isDeleting}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={handleDelete}
              disabled={!confirmed || isDeleting}
            >
              {isDeleting ? "Deleting…" : "Delete account"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Panel>
  );
}

// =============================================================================
// Workspace
// =============================================================================

function WorkspaceSwitcherPanel() {
  const { organization, membership } = useOrganization();
  const role = membership?.role?.replace(/^org:/, "") ?? null;

  return (
    <Panel>
      <PanelHeader
        icon={Building2}
        title="Current workspace"
        description="Workspaces isolate tasks, members, and API keys. Switch or create one from the workspace menu in the top navigation."
      />
      <div className="space-y-4 pt-4">
        <div className="border-border bg-muted/30 flex flex-col gap-3 rounded-lg border p-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0 space-y-1">
            {organization ? (
              <div className="text-muted-foreground flex items-center gap-2 text-[11px] font-medium">
                <span
                  className="inline-block h-1.5 w-1.5 rounded-full bg-[color:var(--paper-pass)]"
                  aria-hidden
                />
                Active workspace
              </div>
            ) : null}
            <p className="font-display truncate text-lg font-medium tracking-tight text-[color:var(--paper-ink)]">
              {organization?.name ?? "No workspace selected"}
            </p>
            {role ? (
              <p className="text-muted-foreground text-xs capitalize">
                Your role:{" "}
                <span className="text-foreground font-medium">{role}</span>
              </p>
            ) : null}
          </div>
        </div>
      </div>
    </Panel>
  );
}

function WorkspaceManagementPanel() {
  const { organization } = useOrganization();

  if (!organization) {
    return (
      <Panel>
        <div className="flex flex-col items-center gap-3 py-10 text-center">
          <div className="border-border bg-muted/40 text-muted-foreground flex h-10 w-10 items-center justify-center rounded-full border border-dashed">
            <Users className="h-5 w-5" />
          </div>
          <div className="space-y-1">
            <p className="text-foreground text-sm font-medium">
              No workspace selected
            </p>
            <p className="text-muted-foreground max-w-sm text-xs">
              Pick a workspace from the menu in the top navigation — or create a
              new one — to manage members, roles, and organization details.
            </p>
          </div>
        </div>
      </Panel>
    );
  }

  return (
    <Panel>
      <PanelHeader
        icon={Users}
        title="Members & organization"
        description={`Manage members, roles, and details for ${organization.name}.`}
      />
      <div className="pt-4">
        <OrganizationProfile
          routing="hash"
          appearance={clerkEmbeddedAppearance}
        />
      </div>
    </Panel>
  );
}

function WorkspaceSection() {
  return (
    <div className="space-y-6">
      <WorkspaceSwitcherPanel />
      <WorkspaceManagementPanel />
    </div>
  );
}

// =============================================================================
// Page shell
// =============================================================================

function SidebarNav({
  section,
  onSelect,
}: {
  section: SettingsSection;
  onSelect: (next: SettingsSection) => void;
}) {
  return (
    <nav
      aria-label="Settings"
      className="flex gap-1 overflow-x-auto pb-2 lg:flex-col lg:gap-0.5 lg:overflow-visible lg:pb-0"
    >
      {SECTIONS.map((entry) => {
        const Icon = entry.icon;
        const active = section === entry.id;
        return (
          <Button
            key={entry.id}
            type="button"
            variant="ghost"
            onClick={() => onSelect(entry.id)}
            aria-current={active ? "page" : undefined}
            className={cn(
              "group h-auto shrink-0 justify-start gap-2.5 rounded-md border border-transparent px-3 py-2 text-left text-sm font-normal lg:w-full",
              active
                ? "border-border bg-card text-foreground hover:bg-card shadow-xs"
                : "text-muted-foreground hover:bg-muted/60 hover:text-foreground"
            )}
          >
            <Icon
              className={cn(
                "h-4 w-4 shrink-0",
                active
                  ? "text-[color:var(--paper-ink)]"
                  : "text-muted-foreground group-hover:text-foreground"
              )}
            />
            <span className="font-medium whitespace-nowrap">{entry.label}</span>
          </Button>
        );
      })}
    </nav>
  );
}

export default function SettingsPage() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  // Accept both `?section=` (new) and `?tab=` (old, to preserve any inbound links).
  const requested =
    searchParams.get("section") ?? searchParams.get("tab") ?? null;
  const section: SettingsSection = isSettingsSection(requested)
    ? requested
    : "profile";

  const currentMeta = SECTIONS.find((entry) => entry.id === section)!;

  const handleSectionChange = (next: SettingsSection) => {
    const params = new URLSearchParams(searchParams.toString());
    // Legacy key cleanup so we end up with one canonical URL shape.
    params.delete("tab");
    if (next === "profile") {
      params.delete("section");
    } else {
      params.set("section", next);
    }

    const query = params.toString();
    router.replace(query ? `${pathname}?${query}` : pathname, {
      scroll: false,
    });
  };

  return (
    <div className="mx-auto w-full max-w-6xl space-y-8 py-2">
      <header className="space-y-2">
        <p className="text-muted-foreground text-xs font-medium tracking-[0.18em] uppercase">
          Settings
        </p>
        <h1 className="font-display text-3xl font-medium tracking-tight text-[color:var(--paper-ink)] sm:text-4xl">
          Account &amp; workspace
        </h1>
        <p className="text-muted-foreground max-w-2xl text-sm leading-relaxed">
          Manage your personal profile, your workspace&rsquo;s members, and the
          API keys that authenticate the Oddish CLI and backend.
        </p>
      </header>

      <div className="grid gap-8 lg:grid-cols-[220px_minmax(0,1fr)]">
        <aside className="lg:sticky lg:top-[calc(5rem+var(--preview-banner-h,0px))] lg:self-start">
          <SidebarNav section={section} onSelect={handleSectionChange} />
        </aside>

        <section className="min-w-0 space-y-6">
          <SectionHeading
            title={currentMeta.label}
            description={currentMeta.description}
          />

          {/*
            All three panels stay mounted so switching sections doesn't
            tear down and re-spin Clerk's <UserProfile> /
            <OrganizationProfile>. We toggle visibility with `hidden`
            (rather than display:none on the parent) so screen readers
            still see the active panel as the live region. The min-h
            keeps the layout from jumping between Account (short) and
            Workspace (tall) on first paint.
          */}
          <div className="relative min-h-[640px]">
            <SectionContainer active={section === "profile"}>
              <ProfilePanel />
            </SectionContainer>
            <SectionContainer active={section === "workspace"}>
              <WorkspaceSection />
            </SectionContainer>
            <SectionContainer active={section === "api-keys"}>
              <APIKeysPanel />
            </SectionContainer>
            <SectionContainer active={section === "byok"}>
              <ByokPanel />
            </SectionContainer>
            <SectionContainer active={section === "notifications"}>
              <NotificationsPanel />
            </SectionContainer>
          </div>
        </section>
      </div>
    </div>
  );
}
