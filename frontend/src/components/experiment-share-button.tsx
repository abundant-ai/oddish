"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";
import { Globe, Copy, Eye, EyeOff, Loader2 } from "lucide-react";
import { fetcher } from "@/lib/api";
import { encodeExperimentRouteParam } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";

interface ExperimentShareInfo {
  name: string;
  is_public: boolean;
  public_token: string | null;
}

export function ExperimentShareButton({
  experimentId,
  canManageShare = true,
}: {
  experimentId: string;
  canManageShare?: boolean;
}) {
  const [isOpen, setIsOpen] = useState(false);
  const [isUpdating, setIsUpdating] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [statusTone, setStatusTone] = useState<"success" | "error" | null>(
    null,
  );

  const encodedId = encodeExperimentRouteParam(experimentId);
  const shareKey = `/api/experiments/${encodedId}/share`;
  // Share metadata is only needed when the publish dialog is opened.
  const { data, mutate } = useSWR<ExperimentShareInfo>(
    isOpen ? shareKey : null,
    fetcher,
  );

  const shareUrl = useMemo(() => {
    if (!data?.public_token || typeof window === "undefined") return null;
    return `${window.location.origin}/share/${data.public_token}`;
  }, [data?.public_token]);

  const handlePublish = async () => {
    if (!canManageShare) return;
    setIsUpdating(true);
    setStatusMessage(null);
    try {
      const res = await fetch(`/api/experiments/${encodedId}/publish`, {
        method: "POST",
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(payload.detail || payload.error || "Failed to publish");
      }
      await mutate(payload, false);
      if (payload.public_token) {
        const url = `${window.location.origin}/share/${payload.public_token}`;
        await navigator.clipboard.writeText(url);
        setStatusTone("success");
        setStatusMessage("Public link copied to clipboard.");
      }
    } catch (error) {
      setStatusTone("error");
      setStatusMessage(
        error instanceof Error
          ? error.message
          : "Unable to publish experiment.",
      );
    } finally {
      setIsUpdating(false);
    }
  };

  const handleUnpublish = async () => {
    if (!canManageShare) return;
    setIsUpdating(true);
    setStatusMessage(null);
    try {
      const res = await fetch(`/api/experiments/${encodedId}/unpublish`, {
        method: "POST",
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(
          payload.detail || payload.error || "Failed to unpublish",
        );
      }
      await mutate(payload, false);
      setStatusTone("success");
      setStatusMessage("Public link disabled.");
    } catch (error) {
      setStatusTone("error");
      setStatusMessage(
        error instanceof Error
          ? error.message
          : "Unable to unpublish experiment.",
      );
    } finally {
      setIsUpdating(false);
    }
  };

  const handleCopy = async () => {
    if (!shareUrl) return;
    try {
      await navigator.clipboard.writeText(shareUrl);
      setStatusTone("success");
      setStatusMessage("Link copied to clipboard.");
    } catch {
      setStatusTone("error");
      setStatusMessage("Failed to copy link.");
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={setIsOpen}>
      <DialogTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className="h-8 gap-2 text-xs font-medium"
          title={
            canManageShare
              ? "Publish experiment"
              : "Only org admins can publish experiments"
          }
          disabled={!canManageShare}
        >
          <Globe className="h-4 w-4" />
          {data?.is_public ? "Public" : "Publish"}
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Publish experiment</DialogTitle>
          <DialogDescription>
            Anyone with the link can view tasks, trials, logs, and files.
          </DialogDescription>
        </DialogHeader>

        {data?.is_public && shareUrl ? (
          <div className="space-y-4 py-2">
            <div className="flex items-center gap-2 text-xs text-emerald-600">
              <Eye className="h-4 w-4" />
              Public link is active
            </div>
            <div className="space-y-2">
              <Label className="text-muted-foreground">Share link</Label>
              <div className="flex items-center gap-2">
                <Input
                  value={shareUrl}
                  readOnly
                  className="font-mono text-xs"
                />
                <Button
                  type="button"
                  variant="outline"
                  size="icon"
                  onClick={handleCopy}
                  title="Copy link"
                >
                  <Copy className="h-4 w-4" />
                </Button>
              </div>
            </div>
            <div className="text-xs text-muted-foreground">
              This view is read-only for anyone with the link.
            </div>
          </div>
        ) : (
          <div className="space-y-3 py-2 text-sm text-muted-foreground">
            <div className="flex items-center gap-2">
              <EyeOff className="h-4 w-4" />
              Not public yet
            </div>
            <div>Publish to generate a shareable read-only link.</div>
          </div>
        )}

        {statusMessage && (
          <div
            className={`text-xs ${
              statusTone === "success" ? "text-emerald-600" : "text-red-500"
            }`}
          >
            {statusMessage}
          </div>
        )}

        <DialogFooter>
          {data?.is_public ? (
            <Button
              type="button"
              variant="outline"
              onClick={handleUnpublish}
              disabled={isUpdating}
            >
              {isUpdating ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  Unpublishing...
                </>
              ) : (
                "Unpublish"
              )}
            </Button>
          ) : (
            <Button type="button" onClick={handlePublish} disabled={isUpdating}>
              {isUpdating ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  Publishing...
                </>
              ) : (
                "Publish"
              )}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
