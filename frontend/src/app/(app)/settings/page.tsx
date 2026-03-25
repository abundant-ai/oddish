"use client";

import { useState } from "react";
import useSWR, { mutate } from "swr";
import {
  CreateOrganization,
  OrganizationProfile,
  UserProfile,
  useOrganization,
  useOrganizationList,
} from "@clerk/nextjs";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
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
  Loader2,
  Plus,
  Trash2,
} from "lucide-react";

const clerkProfileAppearance = {
  variables: {
    colorBackground: "hsl(var(--card))",
    colorText: "hsl(var(--foreground))",
    colorTextSecondary: "hsl(var(--muted-foreground))",
    colorPrimary: "hsl(var(--primary))",
    colorDanger: "hsl(var(--destructive))",
    colorInputBackground: "hsl(var(--background))",
    colorInputText: "hsl(var(--foreground))",
  },
  elements: {
    rootBox: "w-full",
    cardBox: "w-full max-w-none",
    card: "w-full rounded-lg border border-border bg-card text-card-foreground shadow-sm",
    scrollBox: "w-full gap-0",
    navbar: "bg-muted/60 rounded-lg border border-border p-2",
    navbarButton:
      "rounded-md text-sm text-foreground hover:bg-muted data-[active=true]:bg-muted",
    page: "gap-0 border-l-0",
    pageScrollBox: "p-6 border-l-0",
    profilePage: "gap-0",
    dividerRow: "hidden",
    organizationProfilePage: "gap-0",
    headerTitle: "text-foreground",
    headerSubtitle: "text-muted-foreground",
    profileSectionTitle: "text-foreground",
    profileSectionPrimaryButton:
      "bg-primary text-primary-foreground hover:bg-primary/90",
    profileSectionSecondaryButton:
      "border border-border text-foreground hover:bg-muted",
    formButtonPrimary: "bg-primary text-primary-foreground hover:bg-primary/90",
    formFieldLabel: "text-foreground",
    formFieldInput:
      "bg-background border border-input text-foreground focus:ring-2 focus:ring-ring",
    formFieldHintText: "text-muted-foreground",
    formFieldErrorText: "text-destructive",
    badge: "hidden",
  },
};

type SettingsTab = "profile" | "workspace" | "api-keys";

function isSettingsTab(value: string | null): value is SettingsTab {
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
  if (!dateStr) return "Never";
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
    full: "bg-purple-500/10 text-purple-400 border-purple-500/30",
    tasks: "bg-blue-500/10 text-blue-400 border-blue-500/30",
    read: "bg-green-500/10 text-green-400 border-green-500/30",
  };

  return (
    <Badge
      variant="outline"
      className={variants[scope] || "bg-gray-500/10 text-gray-400"}
    >
      {scope}
    </Badge>
  );
}

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
          <DialogTitle>Create API Key</DialogTitle>
          <DialogDescription>
            Create a new API key with scoped access.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <Label htmlFor="api-key-name" className="mb-1 block">
              Name
            </Label>
            <Input
              id="api-key-name"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="My API Key"
              required
            />
          </div>

          <div>
            <Label htmlFor="api-key-scope" className="mb-1 block">
              Scope
            </Label>
            <Select value={scope} onValueChange={setScope}>
              <SelectTrigger id="api-key-scope">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="full">Full - All operations</SelectItem>
                <SelectItem value="tasks">
                  Tasks - Create/view tasks only
                </SelectItem>
                <SelectItem value="read">Read - Read-only access</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div>
            <Label htmlFor="api-key-expiration" className="mb-1 block">
              Expiration (optional)
            </Label>
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
              {isLoading ? "Creating..." : "Create Key"}
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
          <DialogTitle>API Key Created</DialogTitle>
          <DialogDescription>
            Copy your API key now. You won&apos;t be able to see it again!
          </DialogDescription>
        </DialogHeader>

        <div className="flex items-center gap-2 rounded-md border border-border bg-background p-3 font-mono text-sm">
          <code className="flex-1 break-all">{apiKey}</code>
          <Button variant="ghost" size="sm" onClick={handleCopy}>
            {copied ? (
              <Check className="h-4 w-4 text-green-500" />
            ) : (
              <Copy className="h-4 w-4" />
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

function APIKeysCard() {
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
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div>
            <CardTitle className="flex items-center gap-2">
              <Key className="h-5 w-5" />
              API Keys
            </CardTitle>
          </div>
          <Button onClick={() => setShowCreateModal(true)}>
            <Plus className="mr-1 h-4 w-4" />
            Create Key
          </Button>
        </div>
      </CardHeader>
      <CardContent>
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
          <p className="text-muted-foreground">Loading...</p>
        ) : !keys || keys.length === 0 ? (
          <div className="py-8 text-center text-muted-foreground">
            <Key className="mx-auto mb-3 h-12 w-12 opacity-50" />
            <p>No API keys yet</p>
            <p className="text-sm">Create one to get started</p>
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Key</TableHead>
                <TableHead>Scope</TableHead>
                <TableHead>Last Used</TableHead>
                <TableHead>Created</TableHead>
                <TableHead></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {keys.map((key) => (
                <TableRow
                  key={key.id}
                  className={!key.is_active ? "opacity-50" : ""}
                >
                  <TableCell className="font-medium">{key.name}</TableCell>
                  <TableCell className="font-mono text-xs">
                    {key.key_prefix}...
                  </TableCell>
                  <TableCell>
                    <ScopeBadge scope={key.scope} />
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {formatDateTime(key.last_used_at)}
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {formatDate(key.created_at)}
                  </TableCell>
                  <TableCell>
                    {key.is_active && (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setRevokeTarget(key)}
                        disabled={revoking === key.id}
                        className="text-red-400 hover:text-red-300"
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}

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
              <AlertDialogTitle>Revoke API key?</AlertDialogTitle>
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
                {revoking ? "Revoking..." : "Revoke key"}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </CardContent>
    </Card>
  );
}

function ProfileManagementCard() {
  return (
    <div className="space-y-3">
      <div className="space-y-1">
        <h2 className="text-lg font-semibold">Profile & security</h2>
      </div>
      <UserProfile routing="hash" appearance={clerkProfileAppearance} />
    </div>
  );
}

function WorkspaceSelectorCard() {
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const [activatingOrgId, setActivatingOrgId] = useState<string | null>(null);
  const { organization } = useOrganization();
  const { isLoaded, setActive, userMemberships } = useOrganizationList({
    userMemberships: true,
  });

  const memberships = userMemberships.data ?? [];

  const handleSelectWorkspace = async (organizationId: string) => {
    if (!setActive || organization?.id === organizationId) {
      return;
    }

    setActivatingOrgId(organizationId);
    try {
      await setActive({ organization: organizationId });
    } finally {
      setActivatingOrgId(null);
    }
  };

  return (
    <>
      <Card className="border-[#6f88b4]/18 bg-card/95 shadow-sm">
        <CardHeader className="flex flex-row items-start justify-between gap-4 pb-3">
          <div>
            <CardTitle className="text-base">Workspaces</CardTitle>
            <p className="mt-1 text-sm text-muted-foreground">
              Switch between organizations and open the active workspace
              settings.
            </p>
          </div>
          <Button size="sm" onClick={() => setShowCreateDialog(true)}>
            <Plus className="mr-1 h-4 w-4" />
            New
          </Button>
        </CardHeader>
        <CardContent className="space-y-2">
          {!isLoaded || userMemberships.isLoading ? (
            <div className="flex items-center gap-2 rounded-lg border border-border bg-background/70 px-3 py-4 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              Loading workspaces...
            </div>
          ) : memberships.length === 0 ? (
            <div className="rounded-lg border border-dashed border-border bg-background/60 p-4">
              <p className="text-sm text-muted-foreground">
                No workspaces found yet. Create one to get started.
              </p>
            </div>
          ) : (
            memberships.map((membership) => {
              const isActive = organization?.id === membership.organization.id;
              const isSwitching =
                activatingOrgId === membership.organization.id;

              return (
                <Button
                  key={membership.id}
                  type="button"
                  variant="ghost"
                  onClick={() =>
                    handleSelectWorkspace(membership.organization.id)
                  }
                  disabled={isSwitching}
                  className={cn(
                    "h-auto w-full justify-between rounded-xl border px-3 py-3 text-left font-normal transition-colors",
                    isActive
                      ? "hover:bg-[#85b85c]/12 border-[#85b85c]/35 bg-[#85b85c]/10"
                      : "border-[#6f88b4]/16 bg-background/70 hover:border-[#85b85c]/25 hover:bg-muted/60"
                  )}
                >
                  <div className="flex min-w-0 items-center gap-3">
                    <div
                      className={cn(
                        "flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border text-sm font-semibold",
                        isActive
                          ? "bg-[#85b85c]/12 border-[#85b85c]/30 text-[#5c8e43]"
                          : "border-[#6f88b4]/18 bg-muted/50 text-muted-foreground"
                      )}
                    >
                      {membership.organization.name.slice(0, 1).toUpperCase()}
                    </div>
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium text-foreground">
                        {membership.organization.name}
                      </p>
                      <p className="truncate text-xs text-muted-foreground">
                        {membership.role.replace(/^org:/, "")}
                      </p>
                    </div>
                  </div>

                  <div className="ml-3 flex items-center gap-2">
                    {isSwitching ? (
                      <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
                    ) : isActive ? (
                      <Badge
                        variant="outline"
                        className="bg-[#85b85c]/8 border-[#85b85c]/25 text-[#5c8e43]"
                      >
                        Active
                      </Badge>
                    ) : null}
                  </div>
                </Button>
              );
            })
          )}
        </CardContent>
      </Card>

      <Dialog open={showCreateDialog} onOpenChange={setShowCreateDialog}>
        <DialogContent className="max-w-3xl border-0 bg-transparent p-0 shadow-none">
          <CreateOrganization
            routing="hash"
            skipInvitationScreen
            afterCreateOrganizationUrl="/settings?tab=workspace"
            appearance={clerkProfileAppearance}
          />
        </DialogContent>
      </Dialog>
    </>
  );
}

function WorkspaceManagementSection() {
  const { organization } = useOrganization();

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <h2 className="text-lg font-semibold">Workspace settings</h2>
        <p className="text-sm text-muted-foreground">
          Switch between organizations, create new workspaces, and manage
          members from this page.
        </p>
      </div>

      <div className="grid gap-6 xl:grid-cols-[360px_minmax(0,1fr)]">
        <WorkspaceSelectorCard />

        <div>
          {organization ? (
            <OrganizationProfile
              routing="hash"
              appearance={clerkProfileAppearance}
            />
          ) : (
            <Card className="border-[#6f88b4]/28 border-dashed bg-card/70 shadow-none">
              <CardContent className="flex items-center gap-3 p-6 text-sm text-muted-foreground">
                <Building2 className="h-4 w-4 shrink-0" />
                Select a workspace to manage members, roles, and organization
                details.
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}

export default function SettingsPage() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const requestedTab = searchParams.get("tab");
  const tab: SettingsTab = isSettingsTab(requestedTab)
    ? requestedTab
    : "profile";

  const handleTabChange = (nextTab: string) => {
    if (!isSettingsTab(nextTab)) {
      return;
    }

    const params = new URLSearchParams(searchParams.toString());
    if (nextTab === "profile") {
      params.delete("tab");
    } else {
      params.set("tab", nextTab);
    }

    const nextUrl = params.toString()
      ? `${pathname}?${params.toString()}`
      : pathname;
    router.replace(nextUrl, { scroll: false });
  };

  return (
    <div className="space-y-6">
      <Tabs value={tab} onValueChange={handleTabChange} className="space-y-4">
        <TabsList className="grid w-full max-w-xl grid-cols-3">
          <TabsTrigger value="profile">Profile</TabsTrigger>
          <TabsTrigger value="workspace">Workspace</TabsTrigger>
          <TabsTrigger value="api-keys">API Keys</TabsTrigger>
        </TabsList>

        <TabsContent value="profile" className="space-y-6">
          <ProfileManagementCard />
        </TabsContent>

        <TabsContent value="workspace" className="space-y-6">
          <WorkspaceManagementSection />
        </TabsContent>

        <TabsContent value="api-keys" className="space-y-6">
          <div className="grid gap-6">
            <APIKeysCard />
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}
