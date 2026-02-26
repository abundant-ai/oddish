"use client";

import { useEffect, useState } from "react";
import useSWR, { mutate } from "swr";
import { OrganizationProfile, useOrganization } from "@clerk/nextjs";
import { useSearchParams } from "next/navigation";
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
import { Key, Plus, Trash2, Copy, Check } from "lucide-react";

const clerkAppearance = {
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

        <div className="flex items-center gap-2 p-3 bg-background border border-border rounded-md font-mono text-sm">
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
            <Plus className="h-4 w-4 mr-1" />
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
          <div className="text-center py-8 text-muted-foreground">
            <Key className="h-12 w-12 mx-auto mb-3 opacity-50" />
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
                  <TableCell className="text-muted-foreground text-sm">
                    {formatDateTime(key.last_used_at)}
                  </TableCell>
                  <TableCell className="text-muted-foreground text-sm">
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

function OrganizationManagementCard() {
  const { organization, isLoaded } = useOrganization();

  if (!isLoaded) {
    return (
      <div className="rounded-lg border border-border bg-card p-6 shadow-sm">
        <p className="text-muted-foreground">Loading...</p>
      </div>
    );
  }

  if (!organization) {
    return (
      <div className="rounded-lg border border-border bg-card p-6 shadow-sm">
        <p className="text-sm text-muted-foreground">
          Personal accounts do not have organization settings. Select an
          organization from the nav bar to manage members.
        </p>
      </div>
    );
  }

  return <OrganizationProfile routing="hash" appearance={clerkAppearance} />;
}

export default function SettingsPage() {
  const searchParams = useSearchParams();
  const tabParam = searchParams.get("tab");
  const resolvedTab = tabParam === "api-keys" ? "api-keys" : "general";
  const [tab, setTab] = useState(resolvedTab);

  useEffect(() => {
    setTab(resolvedTab);
  }, [resolvedTab]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Settings</h1>
      </div>

      <Tabs value={tab} onValueChange={setTab} className="space-y-4">
        <TabsList>
          <TabsTrigger value="general">Organization</TabsTrigger>
          <TabsTrigger value="api-keys">API Keys</TabsTrigger>
        </TabsList>

        <TabsContent value="general">
          <div className="space-y-6">
            <OrganizationManagementCard />
          </div>
        </TabsContent>
        <TabsContent value="api-keys">
          <div className="grid gap-6">
            <APIKeysCard />
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}
