// Task status (simplified - just tracks trial execution)
export type TaskStatus =
  | "pending"
  | "running"
  | "analyzing"
  | "verdict_pending"
  | "completed"
  | "failed";

// Trial/job status
// - "success": Trial executed to completion (regardless of test result)
// - "failed": Trial encountered an execution error (harness/infrastructure failure)
// - Test results are stored separately in the `reward` field (1=passed, 0=failed, null=no result)
export type TrialStatus =
  | "pending"
  | "queued"
  | "running"
  | "success"
  | "failed"
  | "retrying";

type JobStatus = "pending" | "queued" | "running" | "success" | "failed";

type Priority = "high" | "low";

// Analysis classification for trials (from LLM analysis)
export type AnalysisClassification =
  | "HARNESS_ERROR"
  | "GOOD_FAILURE"
  | "BAD_FAILURE"
  | "GOOD_SUCCESS"
  | "BAD_SUCCESS";

// Trial analysis result
interface TrialAnalysis {
  trial_name?: string;
  classification: AnalysisClassification;
  subtype: string;
  evidence?: string;
  root_cause?: string;
  recommendation?: string;
  reward?: number | null;
}

// Trial
export interface Trial {
  id: string;
  name: string;
  task_id: string;
  task_path: string;
  agent: string;
  provider: string;
  model: string | null;
  status: TrialStatus;
  attempts: number;
  max_attempts: number;
  harbor_stage: string | null;
  reward: number | null;
  error_message?: string | null;
  result?: Record<string, unknown> | null;
  analysis_status?: JobStatus | null;
  analysis?: TrialAnalysis | null;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
}

// Task verdict result (synthesized from trial analyses)
interface TaskVerdict {
  is_good: boolean;
  confidence: "high" | "medium" | "low";
  primary_issue?: string | null;
  recommendations?: string[];
  task_problem_count?: number;
  agent_problem_count?: number;
  success_count?: number;
  harness_error_count?: number;
}

// Task with trials
export interface Task {
  id: string;
  name: string;
  status: TaskStatus;
  priority: Priority;
  user: string;
  github_username?: string | null;
  github_meta?: Record<string, string> | null;
  task_path: string;
  experiment_id: string;
  experiment_name: string;
  experiment_is_public: boolean;
  total: number;
  completed: number;
  failed: number;
  progress?: string;
  reward_success?: number | null;
  reward_total?: number | null;
  run_analysis?: boolean;
  verdict_status?: JobStatus | null;
  verdict?: TaskVerdict | null;
  verdict_error?: string | null;
  trials?: Trial[] | null;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
}

// Queue statistics keyed by queue key
export interface QueueStats {
  [queueKey: string]: {
    pending: number;
    queued: number;
    running: number;
    success: number;
    failed: number;
    retrying: number;
    recommended_concurrency: number;
  };
}

export interface HealthStatus {
  status: "healthy" | "degraded";
  database: "connected" | "disconnected";
  timestamp: string;
}

// Pipeline statistics (analysis/verdict progress)
export interface PipelineStats {
  trials: Record<string, number>;
  analyses: Record<string, number>;
  verdicts: Record<string, number>;
}

// Per-model cost & token usage (aggregated from all trials)
export interface ModelUsage {
  model: string;
  provider: string;
  trial_count: number;
  input_tokens: number;
  cache_tokens: number;
  output_tokens: number;
  cost_usd: number;
  running: number;
  queued: number;
  succeeded: number;
  failed: number;
  avg_duration_s: number | null;
}

export interface DashboardExperimentAuthor {
  name: string;
  source: "github" | "api";
}

export interface DashboardExperiment {
  id: string;
  name: string;
  is_public: boolean;
  task_count: number;
  total_trials: number;
  completed_trials: number;
  failed_trials: number;
  active_trials: number;
  reward_success: number;
  reward_total: number;
  analysis_tasks: number;
  verdict_good: number;
  verdict_needs_review: number;
  verdict_failed: number;
  verdict_pending: number;
  last_created_at: string | null;
  last_author: DashboardExperimentAuthor | null;
  last_pr_url: string | null;
  last_pr_title: string | null;
  last_pr_number: string | null;
}

// Combined dashboard response (single API call)
export interface DashboardResponse {
  health: HealthStatus;
  queues: QueueStats;
  pipeline: PipelineStats;
  model_usage: ModelUsage[];
  tasks: Task[];
  experiments?: DashboardExperiment[];
  tasks_limit?: number;
  tasks_offset?: number;
  has_more?: boolean;
  experiments_limit?: number;
  experiments_offset?: number;
  experiments_total?: number;
  experiments_has_more?: boolean;
  cached: boolean;
}

// =============================================================================
// ATIF Trajectory Types (for step-by-step agent action viewing)
// =============================================================================

interface ToolCall {
  tool_call_id: string;
  function_name: string;
  arguments: Record<string, unknown>;
}

export interface ImageSource {
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

// =============================================================================
// Admin Dashboard Types
// =============================================================================

export interface QueueSlot {
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

export interface PGQueuerJob {
  id: number;
  priority: number;
  created: string | null;
  updated: string | null;
  status: string;
  entrypoint: string;
  payload: Record<string, unknown> | null;
}

export interface PGQueuerStats {
  total: number;
  by_status: Record<string, number>;
  by_entrypoint: Record<string, Record<string, number>>;
}

export interface PGQueuerResponse {
  jobs: PGQueuerJob[];
  stats: PGQueuerStats;
  page: number;
  page_size: number;
  has_more: boolean;
  timestamp: string;
}
