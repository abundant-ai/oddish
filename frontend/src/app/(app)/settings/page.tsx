"use client";

import { useState } from "react";
import useSWR, { mutate } from "swr";
import {
  OrganizationProfile,
  OrganizationSwitcher,
  UserProfile,
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { fetcher } from "@/lib/api";
import { cn } from "@/lib/utils";
import {
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

/**
 * Clerk's `OrganizationSwitcher` trigger sits inline in our custom card;
 * we style it to look like one of our own list rows.
 */
const clerkSwitcherAppearance = {
  variables: {
    colorBackground: "hsl(var(--card))",
    colorText: "hsl(var(--foreground))",
    colorTextSecondary: "hsl(var(--muted-foreground))",
    colorPrimary: "hsl(var(--primary))",
    colorInputBackground: "hsl(var(--background))",
    colorInputText: "hsl(var(--foreground))",
    colorNeutral: "hsl(var(--foreground))",
    borderRadius: "0.5rem",
    fontFamily: "var(--font-sans)",
  },
  elements: {
    rootBox: "w-full",
    organizationSwitcherTrigger:
      "w-full justify-between rounded-md border border-border bg-background px-3 py-2 hover:bg-muted data-[state=open]:bg-muted",
    organizationPreviewMainIdentifier: "text-sm font-medium text-foreground",
    organizationPreviewSecondaryIdentifier:
      "text-xs text-muted-foreground",
    organizationSwitcherTriggerIcon: "text-muted-foreground",
    organizationSwitcherPopoverCard:
      "border border-border shadow-lg rounded-lg",
    organizationSwitcherPopoverActionButton:
      "text-foreground hover:bg-muted",
    avatarBox: "rounded-md border border-border",
  },
};

type SettingsSection = "profile" | "workspace" | "api-keys";

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
    description: "Switch organizations, invite teammates, and manage roles.",
    icon: Building2,
  },
  {
    id: "api-keys",
    label: "API keys",
    description: "Programmatic access tokens for the Oddish CLI and API.",
    icon: Key,
  },
];

function isSettingsSection(value: string | null): value is SettingsSection {
  return value === "profile" || value === "workspace" || value === "api-keys";
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
        "rounded-md font-mono text-[11px] font-medium uppercase tracking-wide",
        variants[scope] ?? "bg-muted text-muted-foreground",
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
        <p className="text-sm leading-relaxed text-muted-foreground">
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
        active
          ? "relative opacity-100"
          : "absolute inset-0 opacity-0",
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
        "rounded-xl border-border/80 bg-card/95 shadow-xs",
        className,
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
    <div className="flex items-start justify-between gap-4 border-b border-border/70 pb-4">
      <div className="flex items-start gap-3">
        {Icon ? (
          <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border bg-muted/60 text-muted-foreground">
            <Icon className="h-4 w-4" />
          </div>
        ) : null}
        <div className="space-y-1">
          <h3 className="text-base font-semibold leading-none tracking-tight text-foreground">
            {title}
          </h3>
          {description ? (
            <p className="text-sm text-muted-foreground">{description}</p>
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
}: {
  isOpen: boolean;
  onClose: () => void;
  onKeyCreated: (key: string) => void;
}) {
  const [name, setName] = useState("");
  const [scope, setScope] = useState("full");
  const [expiresInDays, setExpiresInDays] = useState("never");
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
      setScope("full");
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
                <SelectItem value="full">Full — all operations</SelectItem>
                <SelectItem value="tasks">
                  Tasks — create and view tasks
                </SelectItem>
                <SelectItem value="read">Read — read-only</SelectItem>
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

        <div className="flex items-center gap-2 rounded-md border border-border bg-background p-3 font-mono text-sm">
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

  const {
    data: keys,
    error,
    isLoading,
  } = useSWR<APIKey[]>(`/api/settings/api-keys`, fetcher);

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
          <Button size="sm" onClick={() => setShowCreateModal(true)}>
            <Plus className="mr-1 h-3.5 w-3.5" />
            New key
          </Button>
        }
      />

      <div className="pt-4">
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
          <p className="py-6 text-center text-sm text-muted-foreground">
            Loading…
          </p>
        ) : !keys || keys.length === 0 ? (
          <div className="flex flex-col items-center gap-2 rounded-lg border border-dashed border-border bg-muted/30 py-10 text-center">
            <Key className="h-8 w-8 text-muted-foreground/60" />
            <div className="space-y-0.5">
              <p className="text-sm font-medium text-foreground">
                No API keys yet
              </p>
              <p className="text-xs text-muted-foreground">
                Create one to use the Oddish CLI from your laptop or CI.
              </p>
            </div>
            <Button
              size="sm"
              variant="outline"
              className="mt-2"
              onClick={() => setShowCreateModal(true)}
            >
              <Plus className="mr-1 h-3.5 w-3.5" />
              Create your first key
            </Button>
          </div>
        ) : (
          <div className="overflow-hidden rounded-lg border border-border">
            <Table>
              <TableHeader>
                <TableRow className="bg-muted/40 hover:bg-muted/40">
                  <TableHead className="h-9 text-xs">Name</TableHead>
                  <TableHead className="h-9 text-xs">Key</TableHead>
                  <TableHead className="h-9 text-xs">Scope</TableHead>
                  <TableHead className="h-9 text-xs">Last used</TableHead>
                  <TableHead className="h-9 text-xs">Created</TableHead>
                  <TableHead className="h-9 w-10"></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {keys.map((key) => (
                  <TableRow
                    key={key.id}
                    className={cn(
                      "border-border/70",
                      !key.is_active && "opacity-50",
                    )}
                  >
                    <TableCell className="py-2.5 font-medium">
                      {key.name}
                    </TableCell>
                    <TableCell className="py-2.5 font-mono text-xs text-muted-foreground">
                      {key.key_prefix}…
                    </TableCell>
                    <TableCell className="py-2.5">
                      <ScopeBadge scope={key.scope} />
                    </TableCell>
                    <TableCell className="py-2.5 text-sm text-muted-foreground">
                      {formatDateTime(key.last_used_at)}
                    </TableCell>
                    <TableCell className="py-2.5 text-sm text-muted-foreground">
                      {formatDate(key.created_at)}
                    </TableCell>
                    <TableCell className="py-2.5 text-right">
                      {key.is_active && (
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => setRevokeTarget(key)}
                          disabled={revoking === key.id}
                          className="h-7 w-7 p-0 text-muted-foreground hover:text-destructive"
                          aria-label={`Revoke ${key.name}`}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </div>

      <CreateAPIKeyModal
        isOpen={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        onKeyCreated={(key) => {
          setNewKey(key);
          setShowCreateModal(false);
        }}
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
// Profile
// =============================================================================

function ProfilePanel() {
  return (
    <Panel>
      <PanelHeader
        icon={UserIcon}
        title="Personal account"
        description="Managed by Clerk — update your name, email, password, and connected accounts."
      />
      <div className="pt-4">
        <UserProfile routing="hash" appearance={clerkEmbeddedAppearance} />
      </div>
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
        description="Switch workspaces or create a new one to isolate tasks, members, and API keys."
      />
      <div className="space-y-4 pt-4">
        <div className="flex flex-col gap-3 rounded-lg border border-border bg-muted/30 p-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0 space-y-1">
            {organization ? (
              <div className="flex items-center gap-2 text-[11px] font-medium text-muted-foreground">
                <span
                  className="inline-block h-1.5 w-1.5 rounded-full bg-[color:var(--paper-pass)]"
                  aria-hidden
                />
                Active workspace
              </div>
            ) : null}
            <p className="truncate font-display text-lg font-medium tracking-tight text-[color:var(--paper-ink)]">
              {organization?.name ?? "No workspace selected"}
            </p>
            {role ? (
              <p className="text-xs capitalize text-muted-foreground">
                Your role:{" "}
                <span className="font-medium text-foreground">{role}</span>
              </p>
            ) : null}
          </div>
          <OrganizationSwitcher
            hidePersonal
            afterCreateOrganizationUrl="/settings?section=workspace"
            afterSelectOrganizationUrl="/settings?section=workspace"
            appearance={clerkSwitcherAppearance}
          />
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
          <div className="flex h-10 w-10 items-center justify-center rounded-full border border-dashed border-border bg-muted/40 text-muted-foreground">
            <Users className="h-5 w-5" />
          </div>
          <div className="space-y-1">
            <p className="text-sm font-medium text-foreground">
              No workspace selected
            </p>
            <p className="max-w-sm text-xs text-muted-foreground">
              Pick a workspace above — or create a new one — to manage members,
              roles, and organization details.
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
                ? "border-border bg-card text-foreground shadow-xs hover:bg-card"
                : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
            )}
          >
            <Icon
              className={cn(
                "h-4 w-4 shrink-0",
                active
                  ? "text-[color:var(--paper-ink)]"
                  : "text-muted-foreground group-hover:text-foreground",
              )}
            />
            <span className="whitespace-nowrap font-medium">{entry.label}</span>
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
        <p className="text-xs font-medium uppercase tracking-[0.18em] text-muted-foreground">
          Settings
        </p>
        <h1 className="font-display text-3xl font-medium tracking-tight text-[color:var(--paper-ink)] sm:text-4xl">
          Account &amp; workspace
        </h1>
        <p className="max-w-2xl text-sm leading-relaxed text-muted-foreground">
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
          </div>
        </section>
      </div>
    </div>
  );
}
