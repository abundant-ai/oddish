type TaskStatus =
  | "pending"
  | "running"
  | "analyzing"
  | "verdict_pending"
  | "completed"
  | "failed";

type TrialStatus =
  | "pending"
  | "queued"
  | "running"
  | "success"
  | "failed"
  | "retrying"
  | "skipped";

export type JobStatus = "pending" | "queued" | "running" | "success" | "failed";

type VisibleJobKind = "trial" | "qa" | "analysis";

type VisibleJobStatus =
  | "queued"
  | "running"
  | "retrying"
  | "success"
  | "failed"
  | "cancelled"
  | "blocked";

export interface VisibleWorkerJob {
  id: string;
  kind: VisibleJobKind | string;
  status: VisibleJobStatus | string;
  queue_key: string;
  provider?: string | null;
  external_id?: string | null;
  subject_table?: string | null;
  subject_id?: string | null;
  attempts: number;
  max_attempts: number;
  created_at: string;
  started_at?: string | null;
  claimed_at?: string | null;
  heartbeat_at?: string | null;
  finished_at?: string | null;
  error_message?: string | null;
}

type Priority = "high" | "low";

export type AnalysisClassification =
  | "HARNESS_ERROR"
  | "GOOD_FAILURE"
  | "BAD_FAILURE"
  | "GOOD_SUCCESS"
  | "BAD_SUCCESS";

export interface UserTagRef {
  tag_id: string;
  key: string;
  value?: string | null;
  color?: string | null;
  visibility: "PRIVATE" | "PUBLIC";
  current: boolean;
  older: boolean;
}

export interface TagFilterAST {
  all: string[];
  any: string[];
  none: string[];
}

export interface TagSummary {
  id: string;
  key: string;
  value?: string | null;
  color?: string | null;
  visibility: "PRIVATE" | "PUBLIC";
  state: string;
  usage_count: number;
  row_version: number;
  owner_user_id?: string | null;
  task_count: number;
  version_count: number;
  experiment_count: number;
  owner_label?: string | null;
  owner_avatar_url?: string | null;
}

export interface TagListResponse {
  items: TagSummary[];
}

interface TrialExploitationAssessment {
  links_to: string;
  exploited: boolean;
  exploit_evidence?: string | null;
  causal?: boolean;
}

interface TrialAnalysis {
  trial_name?: string;
  classification: AnalysisClassification;
  subtype: string;
  evidence?: string;
  root_cause?: string;
  recommendation?: string;
  reward?: number | null;
  action_items?: import("./action-items").ActionItem[];
  exploitation?: TrialExploitationAssessment[];
}

interface TrialQueueInfo {
  position?: number | null;
  ahead?: number | null;
  queued_count: number;
  running_count: number;
  concurrency_limit: number;
}

export interface Trial {
  id: string;
  name: string;
  task_id: string;
  task_path: string;
  /** Home experiment — the one whose spend this trial is. */
  experiment_id?: string | null;
  agent: string;
  provider: string;
  model: string | null;
  environment?: string | null;
  status: TrialStatus;
  attempts: number;
  max_attempts: number;
  harbor_stage: string | null;
  harbor_sha?: string | null;
  harbor_source?: string | null;
  reward: number | null;
  error_message?: string | null;
  result?: Record<string, unknown> | null;
  analysis_status?: JobStatus | null;
  analysis?: TrialAnalysis | null;
  superseded_by_trial_id?: string | null;
  jobs?: VisibleWorkerJob[];
  queue_info?: TrialQueueInfo | null;
  task_version?: number | null;
  task_version_id?: string | null;
  input_tokens?: number | null;
  cache_tokens?: number | null;
  output_tokens?: number | null;
  total_steps?: number | null;
  cost_usd?: number | null;
  cost_is_estimated?: boolean | null;
  is_billed?: boolean;
  has_trajectory?: boolean;
  is_probe?: boolean;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  phase_timing?: {
    environment_setup?: {
      started_at: string;
      finished_at: string;
      duration_sec: number;
    };
    agent_setup?: {
      started_at: string;
      finished_at: string;
      duration_sec: number;
    };
    agent_execution?: {
      started_at: string;
      finished_at: string;
      duration_sec: number;
    };
    verifier?: {
      started_at: string;
      finished_at: string;
      duration_sec: number;
    };
  } | null;
}

interface TaskVerdict {
  is_good: boolean;
  confidence: "high" | "medium" | "low";
  primary_issue?: string | null;
  reasoning?: string | null;
  recommendations?: string[];
  task_problem_count?: number;
  agent_problem_count?: number;
  success_count?: number;
  harness_error_count?: number;
}

export interface Task {
  id: string;
  name: string;
  status: TaskStatus;
  priority: Priority;
  user: string;
  github_username?: string | null;
  github_meta?: Record<string, string> | null;
  link?: string | null;
  task_path: string;
  experiment_id: string;
  experiment_name: string;
  experiment_is_public: boolean;
  experiment_created_at?: string | null;
  experiment_owner?: string | null;
  experiment_link?: string | null;
  experiments?: { id: string; name: string }[];
  total: number;
  completed: number;
  failed: number;
  skipped?: number;
  progress?: string;
  reward_success?: number | null;
  reward_sum?: number | null;
  reward_total?: number | null;
  run_analysis?: boolean;
  run_probe?: boolean;
  verdict_status?: JobStatus | null;
  verdict?: TaskVerdict | null;
  verdict_error?: string | null;
  pre_trial_analysis?: { items: import("./action-items").ActionItem[] } | null;
  jobs?: VisibleWorkerJob[];
  current_version?: number | null;
  current_version_id?: string | null;
  trial_version?: number | null;
  trial_version_id?: string | null;
  trials?: Trial[] | null;
  user_tags?: UserTagRef[];
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  finished_at?: string | null;
}

interface TaskBrowseExperiment {
  id: string;
  name: string;
}

interface TaskBrowseTrial {
  id: string;
  name: string;
  status: TrialStatus;
  reward: number | null;
  error_message?: string | null;
}

export interface TaskBrowseItem {
  id: string;
  name: string;
  current_version?: number | null;
  current_version_id?: string | null;
  version_count: number;
  total_trials: number;
  completed_trials: number;
  failed_trials: number;
  reward_success: number;
  reward_sum: number;
  reward_total: number;
  last_run_at?: string | null;
  link?: string | null;
  github_meta?: Record<string, string> | null;
  cost_usd: number;
  cost_trial_count: number;
  cost_has_estimated: boolean;
  cost_has_native: boolean;
  billed_cost_usd: number;
  billed_trial_count: number;
  billed_has_estimated: boolean;
  billed_has_native: boolean;
  latest_trials: TaskBrowseTrial[];
  experiments: TaskBrowseExperiment[];
  user_tags: UserTagRef[];
}

export interface TaskBrowseResponse {
  items: TaskBrowseItem[];
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface TaskBrowseFacets {
  agents: string[];
  models: string[];
  agent_models: { agent: string; model: string | null }[];
  providers: string[];
  environments: string[];
  harbor_stages: string[];
  analysis_classifications: string[];
  experiments: { id: string; name: string }[];
}

export interface TaskVersionSummary {
  id: string;
  version: number;
  message?: string | null;
  created_at: string;
  is_current: boolean;
  trial_count: number;
  completed_count: number;
  failed_count: number;
  skipped_count: number;
  pass_count: number;
  partial_count: number;
  fail_count: number;
  pending_count: number;
  reward_sum: number;
  reward_total: number;
  cost_usd: number;
  cost_trial_count: number;
  cost_has_estimated: boolean;
  cost_has_native: boolean;
  billed_cost_usd: number;
  billed_trial_count: number;
  billed_has_estimated: boolean;
  billed_has_native: boolean;
  last_run_at?: string | null;
  user_tags?: UserTagRef[];
  experiments?: { id: string; name: string }[];
}

interface TaskCostTotals {
  cost_usd: number;
  cost_trial_count: number;
  cost_has_estimated: boolean;
  cost_has_native: boolean;
  billed_cost_usd: number;
  billed_trial_count: number;
  billed_has_estimated: boolean;
  billed_has_native: boolean;
  total_trials: number;
}

/** `GET /api/experiments/{id}/cost-totals` — the experiment's spend rollup.
 *
 * `cost_*` prices every member trial — homed here or gathered into this
 * experiment — i.e. what the work this page renders cost. `owned_*` prices
 * only trials homed in the experiment (the "New spend" tile); it is the
 * number that stays additive across experiments. `billed_*` is the subset of
 * owned spend attributed to a user's quota. Token totals mirror those scopes:
 * `token_*` member-wide, `owned_token_*` home-only, `billed_token_*` the
 * billed subset of owned.
 *
 * All scopes are wider than the grid in two ways: not limited to the trial
 * pages loaded so far, and counting trials the table filters out (earlier
 * task versions, superseded retries, probes). Those still burned tokens and
 * were still billed. Expect this to exceed the sum of the visible rows; the
 * Cost tooltip says as much. */
export interface ExperimentCostTotals {
  cost_usd: number;
  cost_trial_count: number;
  cost_has_estimated: boolean;
  cost_has_native: boolean;
  token_count: number;
  token_trial_count: number;
  owned_cost_usd: number;
  owned_trial_count: number;
  owned_has_estimated: boolean;
  owned_has_native: boolean;
  owned_token_count: number;
  owned_token_trial_count: number;
  billed_cost_usd: number;
  billed_trial_count: number;
  billed_has_estimated: boolean;
  billed_has_native: boolean;
  billed_token_count: number;
  billed_token_trial_count: number;
  total_trials: number;
}

export interface TaskDetailResponse {
  task: Task;
  versions: TaskVersionSummary[];
  totals: TaskCostTotals;
}

export interface QueueStats {
  [queueKey: string]: {
    pending: number;
    queued: number;
    running: number;
    success: number;
    failed: number;
    retrying: number;
    skipped: number;
    recommended_concurrency: number;
  };
}

interface PipelineStats {
  trials: Record<string, number>;
  analyses: Record<string, number>;
  verdicts: Record<string, number>;
}

export interface ModelUsage {
  model: string;
  provider: string;
  trial_count: number;
  input_tokens: number;
  cache_tokens: number;
  output_tokens: number;
  total_steps: number;
  cost_usd: number;
  // Portion of cost_usd that is a token estimate (native cost was missing).
  cost_estimated_usd?: number | null;
  running: number;
  queued: number;
  succeeded: number;
  failed: number;
  avg_duration_s: number | null;
}

export interface JobUsage {
  kind: string;
  queue_key: string;
  job_count: number;
  queued: number;
  running: number;
  retrying: number;
  succeeded: number;
  failed: number;
  cancelled: number;
  blocked: number;
  avg_duration_s: number | null;
}

export interface DashboardExperimentAuthor {
  name: string;
  source: "github" | "api" | "member";
}

export interface OrgUser {
  id: string;
  email: string;
  name: string | null;
  github_username: string | null;
  github_id: string | null;
  role: string;
  org_id: string;
  created_at: string;
}

export interface QuotaUsage {
  user_id: string;
  limit_usd: number;
  used_usd: number;
  reserved_usd?: number;
  enforced?: boolean;
  base_limit_usd?: number;
  bump_usd?: number;
  bump_expires_at?: string | null;
}

export interface QuotaMember extends QuotaUsage {
  email: string;
  name: string | null;
  github_username: string | null;
  role: string;
}

export interface QuotaList {
  members: QuotaMember[];
  // Org-wide monthly cap fields. Absent in a deploy-before-migrate window;
  // treat any as undefined => hide the org section entirely.
  org_limit_usd?: number | null;
  org_used_usd?: number;
  org_reserved_usd?: number;
  org_default_limit_usd?: number | null;
}

export interface QuotaUpdate {
  limit_usd: string | null;
}

export interface QuotaBumpCreate {
  amount_usd: string;
  duration_hours: number;
  reason?: string;
}

// GET /quotas/org — member-visible org monthly budget + adaptive daily goal.
export interface OrgQuotaUsage {
  org_limit_usd: number | null;
  org_used_month_usd: number;
  org_reserved_usd: number;
  org_used_today_usd: number;
  daily_goal_usd: number | null;
  days_remaining: number;
  enforced: boolean;
}

export interface DashboardExperiment {
  id: string;
  name: string;
  is_public: boolean;
  user_tags?: UserTagRef[];
  task_count: number;
  total_trials: number;
  completed_trials: number;
  failed_trials: number;
  skipped_trials: number;
  retrying_trials: number;
  active_trials: number;
  reward_success: number;
  reward_sum: number;
  reward_total: number;
  avg_score: number | null;
  analysis_tasks: number;
  verdict_good: number;
  verdict_needs_review: number;
  verdict_failed: number;
  verdict_pending: number;
  last_created_at: string | null;
  owner_user_id?: string | null;
  last_runner_user_id?: string | null;
  author: DashboardExperimentAuthor | null;
  last_runner: DashboardExperimentAuthor | null;
  last_author: DashboardExperimentAuthor | null;
  last_pr_url: string | null;
  last_pr_title: string | null;
  last_pr_number: string | null;
}

export interface DashboardResponse {
  queues: QueueStats;
  pipeline: PipelineStats;
  model_usage: ModelUsage[];
  job_usage?: JobUsage[];
  tasks: Task[];
  experiments?: DashboardExperiment[];
  tasks_limit?: number;
  tasks_offset?: number;
  has_more?: boolean;
  experiments_limit?: number;
  experiments_offset?: number;
  experiments_has_more?: boolean;
  cached: boolean;
}

interface ToolCall {
  tool_call_id: string;
  function_name: string;
  arguments: Record<string, unknown>;
}

interface ImageSource {
  media_type: string;
  path: string;
}

export interface ContentPart {
  type: "text" | "image";
  text?: string;
  source?: ImageSource;
}

export type MessageContent = string | ContentPart[];
export type ObservationContent = string | ContentPart[] | null;

interface ObservationResult {
  source_call_id: string | null;
  content: ObservationContent;
}

interface Observation {
  results: ObservationResult[];
}

interface StepMetrics {
  prompt_tokens: number | null;
  completion_tokens: number | null;
  cached_tokens: number | null;
  cost_usd: number | null;
}

export interface TrajectoryStep {
  step_id: number;
  timestamp: string | null;
  source: "system" | "user" | "agent";
  model_name: string | null;
  message: MessageContent;
  reasoning_content: string | null;
  tool_calls: ToolCall[] | null;
  observation: Observation | null;
  metrics: StepMetrics | null;
}

interface TrajectoryAgent {
  name: string;
  version: string;
  model_name: string | null;
}

export interface FinalMetrics {
  total_prompt_tokens: number | null;
  total_completion_tokens: number | null;
  total_cached_tokens: number | null;
  total_cost_usd: number | null;
  total_steps: number | null;
}

export interface Trajectory {
  schema_version: string;
  session_id: string;
  agent: TrajectoryAgent;
  steps: TrajectoryStep[];
  notes: string | null;
  final_metrics: FinalMetrics | null;
}

export interface TrajectoryHighlight {
  step_id: number;
  title: string;
  why: string;
}

// Condensed agent-graph summary of a trajectory (GET /trials/{id}/trajectory/graph).
export type TrajectoryGraphStepStatus = "ok" | "warn" | "error";

export type TrajectoryGraphOutcome =
  | "success"
  | "partial"
  | "failure"
  | "timeout"
  | "error"
  | "scoreless"
  | "skipped"
  | "running"
  | "queued"
  | "pending";

export interface TrajectoryGraphStep {
  id: string;
  title: string;
  detail: string;
  status: TrajectoryGraphStepStatus;
}

export interface TrajectoryGraphTerminal {
  outcome: TrajectoryGraphOutcome;
  last_action: string;
  reason: string;
}

export interface TrajectoryGraph {
  headline: string;
  steps: TrajectoryGraphStep[];
  terminal: TrajectoryGraphTerminal;
  source: "summary" | "llm" | "heuristic";
  model: string | null;
  num_steps: number | null;
  schema_version?: string;
  generated_at?: string;
}

export interface TrajectoryPhase {
  label: string;
  gist: string;
  step_ids: number[];
}

export interface TrajectorySummary {
  schema_version: string;
  model: string;
  generated_at: string;
  summary: string;
  highlights: TrajectoryHighlight[];
  phases: TrajectoryPhase[];
}

interface QueueSlot {
  queue_key: string;
  slot: number;
  locked_by: string | null;
  locked_until: string | null;
  is_active: boolean;
}

export interface QueueSlotSummary {
  queue_key: string;
  total_slots: number;
  active_slots: number;
  slots: QueueSlot[];
}

export interface QueueSlotsResponse {
  queue_keys: QueueSlotSummary[];
  total_slots: number;
  total_active: number;
  timestamp: string;
}

interface QueueStatusEntry {
  kind?: string;
  queue_key: string;
  queued: number;
  running: number;
}

export interface QueueStatusResponse {
  queues?: QueueStatusEntry[];
  trial_queues: QueueStatusEntry[];
  analysis_queued: number;
  analysis_running: number;
  verdict_queued: number;
  verdict_running: number;
  timestamp: string;
}

interface OrphanedTrialSample {
  trial_id: string;
  task_id: string;
  queue_key: string;
  status: string;
  issue: string;
  harbor_stage: string | null;
  current_worker_id: string | null;
  current_queue_slot: number | null;
  claimed_at: string | null;
  heartbeat_at: string | null;
  updated_at: string | null;
}

interface OrphanedTaskSample {
  task_id: string;
  status: string;
  run_analysis: boolean;
  verdict_status: string | null;
  issue: string;
  updated_at: string | null;
}

interface OrphanedStateCounts {
  running_stale_heartbeat: number;
  active_tasks_without_active_trials: number;
}

export interface OrphanedStateResponse {
  counts: OrphanedStateCounts;
  trial_samples: OrphanedTrialSample[];
  task_samples: OrphanedTaskSample[];
  stale_after_minutes: number;
  timestamp: string;
}

export type WorkerJobKind =
  | "TRIAL"
  | "QA"
  | "ANALYSIS"
  | "VERDICT"
  | "QA_REVIEW"
  | (string & {});

export type WorkerJobStatus =
  | "QUEUED"
  | "RUNNING"
  | "RETRYING"
  | "SUCCESS"
  | "FAILED"
  | "CANCELLED"
  | "BLOCKED"
  | (string & {});

export interface WorkerJobSample {
  id: string;
  kind: WorkerJobKind;
  status: WorkerJobStatus;
  queue_key: string;
  subject_table: string | null;
  subject_id: string | null;
  attempts: number;
  max_attempts: number;
  claimed_at: string | null;
  heartbeat_at: string | null;
  stale_reaped_at: string | null;
  finished_at: string | null;
  error_message: string | null;
  heartbeat_failure_count: number;
  last_heartbeat_error: string | null;
  current_worker_id: string | null;
  org_id: string | null;
}

interface WorkerJobDurationStat {
  kind: WorkerJobKind;
  queue_key: string;
  sample_count: number;
  p50_seconds: number;
  p95_seconds: number;
}

export interface WorkerJobsResponse {
  counts: Partial<
    Record<WorkerJobKind, Partial<Record<WorkerJobStatus, number>>>
  >;
  stale_running: WorkerJobSample[];
  recent_failures: WorkerJobSample[];
  durations_last_hour: WorkerJobDurationStat[];
  stale_after_minutes: number;
  timestamp: string;
}

interface QueueThroughputStat {
  kind: WorkerJobKind;
  started_5m: number;
  started_15m: number;
  started_60m: number;
  finished_5m: number;
  finished_15m: number;
  finished_60m: number;
}

export interface QueueCapacityStat {
  queue_key: string;
  queued: number;
  queued_scheduled: number;
  running: number;
  limit: number;
  fill: number | null;
  oldest_queued_age_seconds: number | null;
  wait_p50_seconds: number | null;
  wait_p95_seconds: number | null;
}

export interface QueueRuntimeComponentStatus {
  component: string;
  updated_at: string | null;
  age_seconds: number | null;
  payload: Record<string, unknown>;
}

export interface QueueHealthResponse {
  totals_queued: number;
  totals_running: number;
  throughput: QueueThroughputStat[];
  capacity: QueueCapacityStat[];
  dispatcher: QueueRuntimeComponentStatus | null;
  reconciler: QueueRuntimeComponentStatus | null;
  timestamp: string;
}

export interface CostModelBreakdown {
  model: string;
  provider: string;
  trial_count: number;
  input_tokens: number;
  cache_tokens: number;
  output_tokens: number;
  cost_usd: number;
  cost_estimated_usd: number;
}

export interface CostUserBreakdown {
  // Stable grouping key: a user id for billed/submitter rows, else a synthetic
  // "ghid:"/"ghuser:"/"__unattributed__" key for a label-only fallback row.
  key: string;
  // Deep-link target: set for any row backed by a real oddish user (billed or
  // submitter), even if some/all of its spend is unbilled. null for a
  // GitHub-handle / Unattributed fallback row, which renders non-clickable.
  owner_user_id: string | null;
  // True when the row includes trials that were never billed to a quota. Drives
  // the "unbilled" chip; on a linkable row it warns the drilldown total (billed
  // spend only) may be less than this row's total.
  has_unbilled_spend: boolean;
  // Precomputed label for a row with no backing user (GitHub handle,
  // "Unattributed"); null means derive the name from name/email/user id.
  label: string | null;
  org_id: string | null;
  name: string | null;
  email: string | null;
  org_name: string | null;
  trial_count: number;
  experiment_count: number;
  input_tokens: number;
  cache_tokens: number;
  output_tokens: number;
  cost_usd: number;
  cost_estimated_usd: number;
  prev_cost_usd?: number | null;
  inflight_trial_count?: number;
  quota_spent_usd?: number | null;
  quota_limit_usd?: number | null;
  models: CostModelBreakdown[];
}

export interface CostExperimentBreakdown {
  experiment_id: string;
  name: string | null;
  is_deleted: boolean;
  has_deleted_spend: boolean;
  org_id: string | null;
  owner_user_id: string | null;
  owner_name: string | null;
  owner_email: string | null;
  owner_label: string | null;
  org_name: string | null;
  created_at: string | null;
  last_activity_at: string | null;
  trial_count: number;
  input_tokens: number;
  cache_tokens: number;
  output_tokens: number;
  cost_usd: number;
  cost_estimated_usd: number;
  models: CostModelBreakdown[];
}

interface CostSeriesKey {
  key: string;
  label: string;
}

interface CostSeriesBucket {
  bucket_start: string;
  cost_usd: number;
  trial_count: number;
  costs: Record<string, number>;
}

export interface CostSeries {
  dimension: string;
  keys: CostSeriesKey[];
  buckets: CostSeriesBucket[];
}

interface CostTotals {
  window_days: number | null;
  trial_count: number;
  experiment_count: number;
  user_count: number;
  input_tokens: number;
  cache_tokens: number;
  output_tokens: number;
  cost_usd: number;
  cost_native_usd: number;
  cost_estimated_usd: number;
  prev_cost_usd?: number | null;
  month_cost_usd?: number;
  month_budget_usd?: number | null;
}

export interface CostBreakdownResponse {
  window_days: number | null;
  bucket: string;
  series_by_agent: CostSeries;
  series_by_model: CostSeries;
  series_by_user: CostSeries;
  totals: CostTotals;
  by_user: CostUserBreakdown[];
  by_model: CostModelBreakdown[];
  experiments: CostExperimentBreakdown[];
  timestamp: string;
}

export interface CostLeaderboardEntry {
  rank: number;
  name: string;
  cost_usd: number;
}

export interface CostLeaderboardResponse {
  leaders: CostLeaderboardEntry[];
}

// ---------------------------------------------------------------------------
// Admin per-user cost drilldown (GET /api/admin/users/{userId}/costs)
// ---------------------------------------------------------------------------

export interface UserCostTaskBreakdown {
  task_id: string;
  task_name: string | null;
  is_deleted: boolean;
  has_deleted_spend: boolean;
  trial_count: number;
  cost_usd: number;
  cost_estimated_usd: number;
  models: CostModelBreakdown[];
}

export interface UserCostExperimentBreakdown {
  experiment_id: string;
  name: string | null;
  is_deleted: boolean;
  has_deleted_spend: boolean;
  trial_count: number;
  cost_usd: number;
  models: CostModelBreakdown[];
}

interface UserCostTotals {
  window_days: number | null;
  trial_count: number;
  task_count: number;
  experiment_count?: number;
  cost_usd: number;
  cost_estimated_usd: number;
}

export interface UserCostBreakdownResponse {
  billed_user_id: string;
  name: string | null;
  email: string | null;
  github_username: string | null;
  org_id: string | null;
  window_days: number | null;
  bucket: string;
  totals: UserCostTotals;
  tasks: UserCostTaskBreakdown[];
  experiments?: UserCostExperimentBreakdown[];
  series_by_model: CostSeries;
  timestamp: string;
}

export interface PublicExperimentInfo {
  name: string;
  public_token: string;
  description: string | null;
}

export interface ExperimentShareInfo {
  name: string;
  is_public: boolean;
  public_token: string | null;
  description: string | null;
}

export type ReportStatus =
  | "pending"
  | "queued"
  | "running"
  | "success"
  | "failed";

export interface ModelDenominators {
  trials: number;
  scored: number;
  solved: number;
  mean_reward: number | null;
  analyzed: number;
  bad: number;
  good: number;
}

export interface ByModelEntry {
  model: string;
  bucket: "bad" | "good" | "all";
  narrative: string;
  relative_strengths: string;
  relative_weaknesses: string;
  distinctive_failures: string[];
}

export interface ByModel {
  version: number;
  comparison: string;
  denominators: Record<string, ModelDenominators>;
  models: ByModelEntry[];
}

export interface Report {
  id: string;
  name: string;
  status: ReportStatus;
  error?: string | null;
  bad_failure_content?: string | null;
  good_failure_content?: string | null;
  universal_capabilities_content?: string | null;
  headroom_analysis?: string | null;
  num_trials?: number | null;
  num_bad_failures?: number | null;
  num_good_failures?: number | null;
  breakdown?: Record<string, number> | null;
  by_model?: ByModel | null;
  experiment_ids: string[];
  created_at?: string | null;
  finished_at?: string | null;
}

export interface ExperimentOption {
  id: string;
  name: string;
}
