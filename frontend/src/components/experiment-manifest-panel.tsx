"use client";

import { useCallback, useState } from "react";
import { Button } from "@/components/ui/button";
import { Check, ChevronDown, Copy } from "lucide-react";

interface ExperimentManifestPanelProps {
  /** Raw sweep config (the ``-c`` YAML/JSON) the run was generated from. */
  manifest?: string | null;
  /** Verbatim ``oddish run ...`` invocation that generated the run. */
  command?: string | null;
}

function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch (error) {
      console.error("Failed to copy experiment spec", error);
    }
  }, [value]);

  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      onClick={handleCopy}
      className="h-7 gap-1.5 rounded-[6px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-2 font-mono text-[11px] text-[color:var(--paper-ink-2)] transition-colors hover:border-[color:var(--paper-ink-4)] hover:bg-[color:var(--paper-surface-2)] hover:text-[color:var(--paper-ink)]"
      aria-label={copied ? "Copied" : label}
      title={copied ? "Copied" : label}
    >
      {copied ? (
        <Check className="h-3.5 w-3.5" />
      ) : (
        <Copy className="h-3.5 w-3.5" />
      )}
      {copied ? "Copied" : "Copy"}
    </Button>
  );
}

function SpecBlock({
  label,
  value,
  copyLabel,
  wrap,
}: {
  label: string;
  value: string;
  copyLabel: string;
  /** ``true`` wraps long lines (command); ``false`` scrolls horizontally to
   * preserve significant indentation (YAML manifest). */
  wrap: boolean;
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-[10px] font-semibold tracking-[0.09em] text-[color:var(--paper-ink-3)] uppercase">
          {label}
        </span>
        <CopyButton value={value} label={copyLabel} />
      </div>
      <pre
        className={`rounded-[7px] border border-[color:var(--paper-line)] bg-[color:var(--paper-bg-2)] px-3 py-2 font-mono text-[12px] leading-relaxed text-[color:var(--paper-ink)] ${
          wrap
            ? "break-words whitespace-pre-wrap"
            : "max-h-[420px] overflow-auto whitespace-pre"
        }`}
      >
        {value}
      </pre>
    </div>
  );
}

/**
 * Collapsible "Experiment spec" panel: shows the manifest (raw sweep config)
 * and the CLI command the experiment was generated from, so a run can be
 * replicated/tracked.
 *
 * Renders nothing when neither is present. Callers must gate this on the
 * authenticated (non-read-only) view; the public/share API never returns these
 * fields, so a public view receives ``undefined`` and shows nothing.
 */
export function ExperimentManifestPanel({
  manifest,
  command,
}: ExperimentManifestPanelProps) {
  const [open, setOpen] = useState(false);

  const trimmedCommand = command?.trim() ?? "";
  const hasCommand = trimmedCommand.length > 0;
  const hasManifest = Boolean(manifest && manifest.trim().length > 0);

  if (!hasCommand && !hasManifest) return null;

  return (
    <section className="overflow-hidden rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)]">
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-3 px-4 py-2.5 text-left transition-colors hover:bg-[color:var(--paper-surface-2)]"
      >
        <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
          <span className="font-mono text-[11px] font-semibold tracking-[0.09em] text-[color:var(--paper-ink-3)] uppercase">
            Experiment spec
          </span>
          <span className="font-mono text-[11px] text-[color:var(--paper-ink-4)]">
            manifest &amp; command — replicate / track
          </span>
        </span>
        <ChevronDown
          className={`h-4 w-4 shrink-0 text-[color:var(--paper-ink-3)] transition-transform ${
            open ? "rotate-180" : ""
          }`}
          aria-hidden="true"
        />
      </button>

      {open && (
        <div className="space-y-4 border-t border-[color:var(--paper-line-2)] px-4 py-3">
          {hasCommand && (
            <SpecBlock
              label="Command"
              value={trimmedCommand}
              copyLabel="Copy command"
              wrap
            />
          )}
          {hasManifest && (
            <SpecBlock
              label="Manifest"
              value={manifest as string}
              copyLabel="Copy manifest"
              wrap={false}
            />
          )}
        </div>
      )}
    </section>
  );
}
