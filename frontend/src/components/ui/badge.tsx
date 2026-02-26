import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2",
  {
    variants: {
      variant: {
        default:
          "border-transparent bg-primary text-primary-foreground hover:bg-primary/80",
        secondary:
          "border-transparent bg-secondary text-secondary-foreground hover:bg-secondary/80",
        destructive:
          "border-transparent bg-destructive text-destructive-foreground hover:bg-destructive/80",
        outline: "text-foreground",
        // Status variants
        success:
          "border-transparent bg-green-500/20 text-green-400 border-green-500/30",
        warning:
          "border-transparent bg-yellow-500/20 text-yellow-400 border-yellow-500/30",
        info: "border-transparent bg-blue-500/20 text-blue-400 border-blue-500/30",
        pending:
          "border-transparent bg-yellow-500/20 text-yellow-400 border-yellow-500/30",
        queued:
          "border-transparent bg-purple-500/20 text-purple-400 border-purple-500/30",
        running:
          "border-transparent bg-blue-500/20 text-blue-400 border-blue-500/30",
        failed:
          "border-transparent bg-red-500/20 text-red-400 border-red-500/30",
        retrying:
          "border-transparent bg-orange-500/20 text-orange-400 border-orange-500/30",
        completed:
          "border-transparent bg-green-500/20 text-green-400 border-green-500/30",
        // Harbor stage variants
        harborStarting:
          "border-transparent bg-slate-500/10 text-slate-400 border-slate-400/30",
        harborTrialStarted:
          "border-transparent bg-blue-500/10 text-blue-400 border-blue-400/30",
        harborEnvironmentSetup:
          "border-transparent bg-cyan-500/10 text-cyan-400 border-cyan-400/30",
        harborAgentRunning:
          "border-transparent bg-purple-500/10 text-purple-400 border-purple-400/30",
        harborVerification:
          "border-transparent bg-yellow-500/10 text-yellow-400 border-yellow-400/30",
        harborCompleted:
          "border-transparent bg-green-500/10 text-green-400 border-green-400/30",
        harborCleanup:
          "border-transparent bg-gray-500/10 text-gray-400 border-gray-400/30",
        harborCancelled:
          "border-transparent bg-red-500/10 text-red-400 border-red-400/30",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

export interface BadgeProps
  extends
    React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return (
    <span className={cn(badgeVariants({ variant }), className)} {...props} />
  );
}

export { Badge, badgeVariants };
