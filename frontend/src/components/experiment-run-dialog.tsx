"use client";

import { useState } from "react";
import { Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { apiFetch } from "@/lib/api";
import { isAgentTrial, type Task } from "@/lib/types";
import { isBaselineAgentName } from "@/lib/experiment-agent-grouping";
import {
  REASONING_EFFORT_AGENTS,
  reasoningEffortOptions,
} from "@/lib/reasoning-effort";
import {
  buildExperimentRunRequests,
  submitExperimentRuns,
  type ExperimentRunRequest,
} from "@/lib/experiment-run";

export function ExperimentRunDialog({
  experimentId,
  tasks,
  disabled,
  onSubmitted,
}: {
  experimentId: string;
  tasks: Task[];
  disabled: boolean;
  onSubmitted?: (taskIds?: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const [agent, setAgent] = useState("claude-code");
  const [model, setModel] = useState("");
  const [efforts, setEfforts] = useState(["default"]);
  const [repetitions, setRepetitions] = useState("5");
  const [pending, setPending] = useState<ExperimentRunRequest[] | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [completed, setCompleted] = useState(0);
  const [errors, setErrors] = useState<string[]>([]);
  const solverTrials = tasks
    .flatMap((task) => task.trials ?? [])
    .filter(
      (trial) =>
        isAgentTrial(trial) &&
        !trial.is_probe &&
        !isBaselineAgentName(trial.agent)
    );
  const agents = [
    ...new Set([
      ...REASONING_EFFORT_AGENTS,
      ...solverTrials.map((trial) => trial.agent),
    ]),
  ];
  const models = [
    ...new Set(
      solverTrials
        .filter((trial) => trial.agent === agent)
        .map((trial) => trial.model)
        .filter((value): value is string => !!value)
    ),
  ];
  const options = reasoningEffortOptions(agent, model);
  const n = Number(repetitions);
  const valid =
    model.trim() &&
    efforts.length > 0 &&
    Number.isInteger(n) &&
    n >= 1 &&
    n <= 100;
  const total = tasks.length * efforts.length * (valid ? n : 0);

  function start() {
    const first =
      solverTrials.find(
        (trial) =>
          reasoningEffortOptions(trial.agent, trial.model ?? "").length > 0
      ) ?? solverTrials[0];
    setAgent(first?.agent ?? "claude-code");
    setModel(first?.model ?? "");
    setEfforts(
      reasoningEffortOptions(
        first?.agent ?? "claude-code",
        first?.model ?? ""
      ).includes("high")
        ? ["high"]
        : ["default"]
    );
    setPending(null);
    setErrors([]);
    setCompleted(0);
    setOpen(true);
  }

  async function submit() {
    if (submitting || (!pending && !valid)) return;
    const requests =
      pending ??
      buildExperimentRunRequests(
        tasks.map((task) => task.id),
        experimentId,
        agent,
        model,
        efforts,
        n,
        crypto.randomUUID()
      );
    setPending(requests);
    setSubmitting(true);
    setCompleted(0);
    setErrors([]);
    try {
      const result = await submitExperimentRuns(
        requests,
        apiFetch,
        setCompleted
      );
      setPending(result.failed);
      setErrors(result.errors);
      const failedIds = new Set(result.failed.map((request) => request.taskId));
      onSubmitted?.(
        requests
          .filter((request) => !failedIds.has(request.taskId))
          .map((request) => request.taskId)
      );
      if (result.failed.length === 0) setOpen(false);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <>
      <Button
        variant="outline"
        size="sm"
        disabled={disabled || tasks.length === 0}
        onClick={start}
      >
        <Plus className="h-4 w-4" />
        Run trials
      </Button>
      <Dialog
        open={open}
        onOpenChange={(next) => {
          if (!submitting) setOpen(next);
        }}
      >
        <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-xl">
          <DialogHeader>
            <DialogTitle>Run trials</DialogTitle>
            <DialogDescription>
              Add trials to this experiment · {tasks.length} tasks
            </DialogDescription>
          </DialogHeader>
          <fieldset
            disabled={submitting || pending !== null}
            className="space-y-5 disabled:opacity-70"
          >
            <div className="grid gap-4 sm:grid-cols-[1fr_1.6fr]">
              <label className="space-y-2 text-sm">
                Agent
                <Select
                  value={agent}
                  onValueChange={(value) => {
                    setAgent(value);
                    const nextModel =
                      solverTrials.find((trial) => trial.agent === value)
                        ?.model ?? "";
                    setModel(nextModel);
                    setEfforts(
                      reasoningEffortOptions(value, nextModel).includes("high")
                        ? ["high"]
                        : ["default"]
                    );
                  }}
                >
                  <SelectTrigger className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {agents.map((value) => (
                      <SelectItem key={value} value={value}>
                        {value}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </label>
              <label className="space-y-2 text-sm">
                Model
                <Input
                  value={model}
                  onChange={(event) => {
                    setModel(event.target.value);
                    setEfforts(
                      reasoningEffortOptions(
                        agent,
                        event.target.value
                      ).includes("high")
                        ? ["high"]
                        : ["default"]
                    );
                  }}
                  list="experiment-run-models"
                  placeholder="Model ID"
                  className="font-mono text-xs"
                />
                <datalist id="experiment-run-models">
                  {models.map((value) => (
                    <option key={value} value={value} />
                  ))}
                </datalist>
              </label>
            </div>
            <div>
              <div className="mb-2 text-sm">Reasoning effort</div>
              <div className="flex flex-wrap gap-2">
                {(options.length ? options : ["default"]).map((effort) => (
                  <label
                    key={effort}
                    className="flex cursor-pointer items-center gap-2 rounded-md border px-3 py-2 text-xs"
                  >
                    <Checkbox
                      checked={efforts.includes(effort)}
                      onCheckedChange={(checked) =>
                        setEfforts(
                          checked
                            ? [...efforts, effort]
                            : efforts.filter((value) => value !== effort)
                        )
                      }
                    />
                    {effort === "default" ? "Agent default" : effort}
                  </label>
                ))}
              </div>
              <p className="text-muted-foreground mt-2 text-xs">
                {options.length
                  ? "New runs default to high. Each selected effort gets its own column."
                  : "Effort selection is unavailable for this agent/model."}
              </p>
            </div>
            <label className="block max-w-40 space-y-2 text-sm">
              Trials per effort
              <Input
                type="number"
                min={1}
                max={100}
                value={repetitions}
                onChange={(event) => setRepetitions(event.target.value)}
              />
            </label>
          </fieldset>
          <div
            className="flex items-center justify-between gap-3 border-y py-4 text-sm"
            aria-live="polite"
          >
            <span>
              {tasks.length} tasks × {efforts.length} efforts ×{" "}
              {repetitions || 0} trials
            </span>
            <span className="text-xl whitespace-nowrap">
              {total.toLocaleString()} trials
            </span>
          </div>
          {errors.length > 0 && (
            <div role="alert" className="space-y-2 text-sm">
              <p>
                {pending?.length} task submissions failed. Successful
                submissions are kept; retry sends only the failed requests.
              </p>
              <ul className="text-destructive max-h-32 list-inside list-disc overflow-y-auto text-xs break-words">
                {errors.map((error, index) => (
                  <li key={index}>{error}</li>
                ))}
              </ul>
            </div>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              disabled={submitting}
              onClick={() => setOpen(false)}
            >
              Cancel
            </Button>
            <Button
              onClick={() => void submit()}
              disabled={submitting || (!pending && !valid)}
            >
              {submitting
                ? `Submitting ${completed}/${pending?.length ?? tasks.length} tasks…`
                : pending
                  ? `Retry ${pending.length} tasks`
                  : `Run ${total.toLocaleString()} trials`}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
