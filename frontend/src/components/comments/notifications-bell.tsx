"use client";

import {
  ClientSideSuspense,
  useInboxNotificationThread,
  useInboxNotifications,
  useMarkAllInboxNotificationsAsRead,
  useUnreadInboxNotificationsCount,
} from "@liveblocks/react/suspense";
import { InboxNotification } from "@liveblocks/react-ui";
import type { InboxNotificationData } from "@liveblocks/client";
import { Bell } from "lucide-react";
import { formatLineRange } from "@/lib/line-range";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { CommentsErrorBoundary } from "@/components/comments/comments-provider";

/**
 * Nav-bar bell for QA comment notifications (mentions and thread replies),
 * backed by the Liveblocks inbox. Opening the menu marks everything read;
 * items deep-link to the commented task, file, and lines via the
 * ``?drawer=task&taskFile=&taskLines=`` address the task page hydrates
 * from the URL.
 */
export function NotificationsBell() {
  return (
    <CommentsErrorBoundary>
      <ClientSideSuspense fallback={null}>
        <BellInner />
      </ClientSideSuspense>
    </CommentsErrorBoundary>
  );
}

function BellInner() {
  const { inboxNotifications } = useInboxNotifications();
  const { count: unreadCount } = useUnreadInboxNotificationsCount();
  const markAllRead = useMarkAllInboxNotificationsAsRead();

  return (
    <DropdownMenu
      modal={false}
      onOpenChange={(open) => {
        if (open && unreadCount > 0) markAllRead();
      }}
    >
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          aria-label={`Notifications${unreadCount ? ` (${unreadCount} unread)` : ""}`}
          className="relative px-2"
        >
          <Bell className="h-4 w-4" />
          {unreadCount > 0 && (
            <span className="bg-destructive text-destructive-foreground absolute -top-0.5 -right-0.5 flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-[9px] font-semibold">
              {unreadCount > 99 ? "99+" : unreadCount}
            </span>
          )}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-96 border-[#6f88b4]/20 p-2">
        {inboxNotifications.length === 0 ? (
          <p className="text-muted-foreground px-2 py-4 text-center text-xs">
            No notifications yet. You&apos;ll see @mentions and replies on QA
            comments here.
          </p>
        ) : (
          <div className="max-h-96 overflow-y-auto">
            {inboxNotifications.map((notification) =>
              notification.kind === "thread" ? (
                // Each item carries its own suspense + error scope so one
                // broken thread payload degrades to a plain notification
                // instead of blanking the whole inbox.
                <CommentsErrorBoundary
                  key={notification.id}
                  fallback={
                    <InboxNotification inboxNotification={notification} />
                  }
                >
                  <ClientSideSuspense fallback={null}>
                    <ThreadNotification notification={notification} />
                  </ClientSideSuspense>
                </CommentsErrorBoundary>
              ) : (
                <InboxNotification
                  key={notification.id}
                  inboxNotification={notification}
                />
              )
            )}
          </div>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function ThreadNotification({
  notification,
}: {
  notification: InboxNotificationData;
}) {
  const thread = useInboxNotificationThread(notification.id);

  // Room ids are `qa:{orgId}:{taskId}`; the file/line anchor rides on the
  // thread metadata. Plain anchor = hard navigation, which is deliberate:
  // the task page hydrates its drawer state from the URL once per mount,
  // so a soft transition to an already-open task page would not act on
  // the new address.
  const taskId = notification.roomId?.split(":").slice(2).join(":");
  let href: string | undefined;
  if (taskId) {
    const params = new URLSearchParams({
      drawer: "task",
      taskFile: thread.metadata.filePath,
    });
    if (thread.metadata.lineStart != null && thread.metadata.lineEnd != null) {
      params.set(
        "taskLines",
        formatLineRange({
          start: thread.metadata.lineStart,
          end: thread.metadata.lineEnd,
        })
      );
    }
    href = `/tasks/${encodeURIComponent(taskId)}?${params.toString()}`;
  }

  return <InboxNotification inboxNotification={notification} href={href} />;
}
