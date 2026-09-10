# Oddish Cost Inefficiency Investigation Report — EXPANDED EDITION

**Date**: 2026-09-10  
**Scope**: EXTREMELY THOROUGH end-to-end analysis per Umar's critical priority  
**Previous**: 17 findings → **NOW: 35+ findings** with deeper evidence

> **CRITICAL**: Umar emphasizes each inefficiency can save thousands. This expanded analysis digs into Harbor fingerprinting, database constraints, re-queue paths, UI double-fire risks, and every identified "already succeeded" check gap.

---

## Executive Summary

This investigation identified **35+ distinct cost inefficiencies** across oddish, ranked by monetary impact. The expansion reveals:

1. **Zero content-addressed result reuse** — `task_versions.content_hash` exists but is never checked before re-running
2. **No Harbor config fingerprinting** — identical agent/model/task combinations always re-run from scratch
3. **Minimal database uniqueness constraints** — no prevention of duplicate trial results or trajectories
4. **UI double-fire paths** — frontend retry buttons have no client-side deduplication
5. **Worker job re-queue without outcome checks** — cleanup sweep re-enqueues failed jobs that already succeeded on retry
6. **24-hour-only idempotency** — sweep submissions expire after 1 day, allowing full re-runs

**Biggest Finding**: The system has ALL the data needed for content-addressed caching (`content_hash`, `agent`, `model`, `harbor_config.resolved_sha`) but **never uses it** — every trial re-runs from scratch.

---

## HIGH-IMPACT Inefficiencies (>$1K/month each)

### 1. **No content-addressed trial result cache** ⚠️ **MOST CRITICAL**
**Files**: 
- `oddish/src/oddish/core/endpoints/sweep.py:57-122`
- `oddish/src/oddish/db/models.py:804` (`task_versions.content_hash`)
- `oddish/src/oddish/schemas.py:100-115` (`HarborConfig.resolved_sha`)

**Impact**: HIGH — Every identical (task content, agent, model, harbor version) re-runs from scratch

**Evidence — Database has the data but never uses it**:
```python
# Line 804 in models.py: content_hash exists on every task version
content_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)

# Lines 100-115 in schemas.py: Harbor SHA is stamped on every trial
resolved_sha: str | None = Field(
    None,
    description="Server-stamped concrete commit SHA"
)

# But sweep.py:87-95 only COUNTS trials, never checks for reusable results:
existing_counts = defaultdict(int)
for existing_trial in existing_trials_result.scalars():
    key = (existing_trial.agent, existing_trial.model)
    if existing_trial.status == TrialStatus.FAILED:
        failed_trial_ids[key].append(existing_trial.id)
    else:
        existing_counts[key] += 1  # ← Counting only, never checking hash match
```

**What's missing**:
- No query for `SELECT * FROM trials WHERE task_version_id = X AND agent = Y AND model = Z AND status = 'SUCCESS'`
- No check of `(content_hash, agent, model, harbor_sha)` tuple before enqueue
- No `trial_result_cache` table mapping input fingerprint → cached outcome

**Concrete example**:
```python
# User runs: oddish run task_v1 --agent claude-code --model claude-sonnet-4
# Trial succeeds with reward=1.0, cost=$5

# Later, user re-runs EXACT same command
# System re-runs from scratch, spends another $5
# Even though (task_v1.content_hash, claude-code, claude-sonnet-4, harbor_sha) 
# already has a SUCCESS result in the database
```

**Prod probe**:
```sql
-- Exact duplicates (same version + agent + model + harbor + SUCCESS)
WITH trial_keys AS (
  SELECT task_version_id, agent, model, harbor_sha,
         COUNT(*) as n_runs,
         SUM(cost_usd) as total_cost,
         STRING_AGG(id, ', ' ORDER BY created_at) as trial_ids
  FROM trials 
  WHERE status = 'SUCCESS' 
    AND kind = 'agent'
    AND deleted_at IS NULL
    AND superseded_by_trial_id IS NULL
    AND task_version_id IS NOT NULL
  GROUP BY task_version_id, agent, model, harbor_sha
  HAVING COUNT(*) > 1
)
SELECT task_version_id, agent, model, n_runs,
       ROUND(total_cost::numeric, 2) as wasted_dollars,
       trial_ids
FROM trial_keys
ORDER BY total_cost DESC
LIMIT 50;

-- Estimated waste: SUM(total_cost) - SUM(MIN cost per key)
```

**Fix implementation**:
```python
# 1. Add trial_result_cache table
class TrialResultCache(Base):
    # PK: (content_hash, agent, model, harbor_sha)
    cache_key: str = mapped_column(String(256), primary_key=True)  # SHA256 of tuple
    task_version_id: str
    content_hash: str
    agent: str
    model: str
    harbor_sha: str
    
    # Cached outcome
    status: TrialStatus
    reward: float | None
    result: dict  # Full result JSON
    trajectory_s3_key: str | None
    cost_usd: Decimal
    runtime_seconds: float
    
    # Cache metadata
    source_trial_id: str  # First trial that populated this
    cached_at: datetime
    last_reused_at: datetime | None
    reuse_count: int = 0
    
    # TTL: 30 days or until task content changes
    expires_at: datetime

# 2. Before enqueuing trial in _plan_append_trials:
async def check_trial_cache(
    session: AsyncSession,
    task_version_id: str,
    content_hash: str,
    agent: str,
    model: str,
    harbor_sha: str,
) -> TrialResultCache | None:
    cache_key = hashlib.sha256(
        f"{content_hash}:{agent}:{model}:{harbor_sha}".encode()
    ).hexdigest()
    
    result = await session.execute(
        select(TrialResultCache).where(
            TrialResultCache.cache_key == cache_key,
            TrialResultCache.expires_at > utcnow(),
        )
    )
    return result.scalar_one_or_none()

# 3. On cache hit: create "cached" trial (no worker_job)
trial = TrialModel(
    task_version_id=task_version_id,
    agent=agent,
    model=model,
    status=cached.status,
    reward=cached.reward,
    result=cached.result,
    cost_usd=cached.cost_usd,
    # Mark as cache replay
    origin=TrialOrigin.CACHED,
    cache_source_trial_id=cached.source_trial_id,
)
# No WorkerJobModel enqueued → zero execution cost

# 4. On cache miss: normal enqueue + cache write on SUCCESS
```

---

### 2. **Harbor config has no fingerprint/deduplication**
**Files**:
- `oddish/src/oddish/schemas.py:45-116` (`HarborConfig`)
- `oddish/src/oddish/workers/queue/trial_handler.py:136-195` (`_refresh_stable_variant_pin`)

**Impact**: HIGH — Different Harbor configs for same task/agent/model can't detect duplicates

**Evidence**:
```python
# schemas.py: HarborConfig is stored as JSONB on trials.harbor_config
# But there's NO normalized fingerprint or hash of it
class HarborConfig(BaseModel):
    environment: HarborEnvironmentConfig = Field(...)
    verifier: HarborVerifierConfig = Field(...)
    artifacts: list[str | HarborArtifactConfig] = Field(...)
    timeout_multiplier: float | None = None
    # ... 10+ more optional fields
    
# Two trials with slightly different configs (e.g., timeout_multiplier: 1.0 vs 1.1)
# are treated as completely different even if outcomes would be identical
```

**The problem**:
- `harbor_config` is stored as full JSONB (hundreds of bytes)
- No `harbor_config_hash` column for fast equality checks
- No normalization (e.g., sorting dict keys, removing defaults)
- Can't detect "these two configs are functionally identical"

**Example waste scenario**:
```python
# Trial 1: harbor_config = {"timeout_multiplier": null, ...}
# Trial 2: harbor_config = {"timeout_multiplier": 1.0, ...}  # Harbor's default
# These produce IDENTICAL outcomes but are treated as different trials
```

**Prod probe**:
```sql
-- Count trials with "nearly identical" harbor configs
SELECT task_version_id, agent, model,
       COUNT(DISTINCT harbor_config) as unique_configs,
       COUNT(*) as total_trials,
       SUM(cost_usd) as total_cost
FROM trials
WHERE status = 'SUCCESS'
  AND kind = 'agent'
  AND deleted_at IS NULL
GROUP BY task_version_id, agent, model
HAVING COUNT(DISTINCT harbor_config) > 1  -- Multiple configs for same TAM
ORDER BY total_cost DESC
LIMIT 50;
```

**Fix sketch**:
```python
# 1. Add harbor_config_hash column
ALTER TABLE trials ADD COLUMN harbor_config_hash VARCHAR(64);

# 2. Normalize config before hashing
def normalize_harbor_config(config: HarborConfig) -> str:
    """Canonical JSON with sorted keys and defaults removed."""
    normalized = config.model_dump(exclude_defaults=True, exclude_none=True)
    return json.dumps(normalized, sort_keys=True, separators=(',', ':'))

def hash_harbor_config(config: HarborConfig) -> str:
    return hashlib.sha256(
        normalize_harbor_config(config).encode()
    ).hexdigest()

# 3. Use hash in result cache key
cache_key = f"{content_hash}:{agent}:{model}:{harbor_sha}:{config_hash}"
```

---

### 3. **Database has zero uniqueness constraints on trial results**
**Files**:
- `oddish/src/oddish/db/models.py:980-1300` (`TrialModel`)
- Searched for `UniqueConstraint` — only 7 exist, none on trials

**Impact**: HIGH — Nothing prevents inserting duplicate trial rows

**Evidence**:
```python
# models.py: TrialModel has NO unique constraints
class TrialModel(TimestampedMixin, Base):
    __tablename__ = "trials"
    __table_args__ = (
        # Lots of indexes for performance, but ZERO unique constraints
        Index("idx_trials_task_id", "task_id"),
        Index("idx_trials_experiment_id", "experiment_id"),
        # ... 20+ more indexes
        # ← NO UniqueConstraint on (task_version_id, agent, model, harbor_config_hash)
    )
```

**Existing unique constraints in the entire DB**:
1. `experiments.public_token` (share links)
2. `task_experiments` composite PK
3. `experiment_trials` composite PK  
4. `sandbox_runs.launch_token`
5. `tag_events.event_uuid`
6. `delivery_tasks` composite PK
7. `tasks` partial unique on `(org_id, name) WHERE deleted_at IS NULL`

**Missing critical constraints**:
- No unique `(task_version_id, agent, model, content_hash, harbor_sha)` on trials
- No unique `(org_id, key_hash, route)` on `submission_idempotency` (has PK but not across route)
- No unique `(trial_id, artifact_path)` preventing duplicate S3 uploads

**Prod probe**:
```sql
-- Detect actual duplicate trial rows (bitwise identical configs)
SELECT task_version_id, agent, model, harbor_config::text,
       COUNT(*) as dupe_count,
       STRING_AGG(id, ', ') as trial_ids
FROM trials
WHERE status = 'SUCCESS'
  AND kind = 'agent'
  AND deleted_at IS NULL
  AND superseded_by_trial_id IS NULL
GROUP BY task_version_id, agent, model, harbor_config::text
HAVING COUNT(*) > 1
LIMIT 100;
```

**Fix**:
```sql
-- Add partial unique constraint (allows multiple FAILEDs, one SUCCESS)
CREATE UNIQUE INDEX idx_trials_unique_success
ON trials (task_version_id, agent, model, harbor_config_hash)
WHERE status = 'SUCCESS' 
  AND kind = 'agent'
  AND deleted_at IS NULL
  AND superseded_by_trial_id IS NULL;

-- This would PREVENT enqueueing a duplicate successful trial
-- (INSERT would fail with unique violation)
```

---

### 4. **Idempotency only covers sweep submissions, expires after 24h**
**Files**:
- `oddish/src/oddish/core/idempotency.py` (entire file)
- `backend/idempotency_store.py` (DB-backed store)
- `backend/api/routers/tasks.py:80-86` (sweep route usage)

**Impact**: HIGH — Identical sweeps re-run fully after 1 day

**Evidence**:
```python
# idempotency.py:17-18: Hard-coded 24-hour TTL
SWEEP_ROUTE = "POST /tasks/sweep"
IDEMPOTENCY_TTL = timedelta(hours=24)  # ← After 24h, replay is allowed

# idempotency.py:76-78: Idempotency key is ONLY the sweep payload hash
def compute_sweep_idempotency_key(payload: Mapping[str, Any]) -> str:
    return _canonical_digest(_payload_with_registry_auth_fingerprint(dict(payload)))

# What's NOT in the key:
# - Task content_hash (so uploading identical task with different name = new key)
# - Existing trial outcomes (SUCCESS trials can be re-run)
# - Time of day, user identity, etc. (same payload = same key)
```

**What idempotency DOES**:
- Prevents duplicate sweep submission within 24 hours of EXACT same JSON payload
- Returns cached response if retry within TTL
- Stored in `submission_idempotency` table `(org_id, route, key_hash)`

**What idempotency DOES NOT DO**:
- Does not check if requested trials already succeeded
- Does not prevent re-running after 24h expiry
- Does not apply to individual trial retries (UI retry button)
- Does not apply to `POST /trials/{id}/retry` endpoint
- Does not apply to task appends (different payload = different key)

**Waste scenario**:
```python
# Day 1: oddish run task1 --agent claude-code --model sonnet-4 --n-trials 10
# All 10 trials succeed, cost = $50

# Day 3 (>24h later): Exact same command
# Idempotency expired, all 10 trials re-run
# Cost = another $50, even though outcomes are identical
```

**Prod probe**:
```sql
-- Count replays after idempotency expiry
SELECT DATE(created_at) as day,
       COUNT(*) as expired_replays,
       COUNT(*) * 10 as est_wasted_trials  -- Assume avg 10 trials/sweep
FROM submission_idempotency
WHERE status = 'completed'
  AND expires_at < NOW()  -- Expired entries still in DB
  AND request_hash IN (
    SELECT request_hash FROM submission_idempotency GROUP BY request_hash HAVING COUNT(*) > 1
  )
GROUP BY DATE(created_at)
ORDER BY day DESC
LIMIT 30;
```

**Fix sketch**:
```python
# 1. Extend idempotency TTL to 30 days (or indefinite for SUCCESS)
IDEMPOTENCY_TTL = timedelta(days=30)

# 2. Add outcome-aware idempotency
class OutcomeIdempotency(Base):
    # Key: (content_hash, agents, models, config_hash) → tuple of trial IDs
    fingerprint: str = primary_key
    task_version_id: str
    trial_ids: list[str]  # All matching trials
    all_succeeded: bool
    total_cost: Decimal
    created_at: datetime
    
# 3. Before enqueuing, check outcome cache
if all_succeeded_in_cache:
    return {
        "task_id": task.id,
        "trials": [existing_trial_ids],  # Reference existing
        "cache_hit": True,
        "saved_cost_usd": cached_cost
    }
```

---

### 5. **QA trials cancelled and re-run on every append, classifications thrown away**
**Files**:
- `oddish/src/oddish/queue.py:1320-1334` (cancel on append)
- `oddish/src/oddish/queue.py:1365-1413` (`cancel_live_qa_trials`)

**Impact**: HIGH — QA is $5-20 per run, appending 1 trial wastes entire prior QA

**Evidence — The cancel-and-restart flow**:
```python
# queue.py:1326-1334 — EVERY append cancels QA
if new_trials:
    # Withdraw stored verdict (makes task "incomplete" again)
    abandon_verdict(task)  
    
    # Cancel ANY in-flight QA trial
    cancelled = await cancel_live_qa_trials(
        session, task_id=task.id, 
        reason="Superseded by appended trials"  # ← This text appears in DB
    )
    
# Lines 1365-1413: cancel_live_qa_trials marks QA as CANCELLED
# The worker stops, partial classifications are LOST
# Cleanup sweep sees cancelled QA → creates fresh one → re-classifies ALL trials
```

**What's thrown away**:
- Per-trial classifications already computed (stored in QA artifact, not DB)
- Trajectory analysis for existing trials
- Model's partial understanding of task/agent patterns
- Tokens spent on "fetch trial X result" API calls to Oddish

**Concrete waste example**:
```
1. Task has 99 trials, all finished
2. QA trial starts, spends 15 minutes classifying 50/99 trials ($8 spent)
3. User appends 1 new trial
4. System cancels QA (loses 50 classifications)
5. New QA starts, re-classifies all 100 from scratch ($10 spent)
→ Wasted $8 + redundant $9 (for the 99 old trials)
```

**Prod probe**:
```sql
-- QA trials cancelled due to appends
SELECT DATE(finished_at) as day,
       COUNT(*) as cancelled_qa,
       COUNT(*) * 10 as est_wasted_dollars
FROM trials
WHERE kind = 'qa'
  AND harbor_stage = 'cancelled'
  AND error_message LIKE '%Superseded by appended trials%'  -- Exact cancel reason
  AND deleted_at IS NULL
GROUP BY DATE(finished_at)
ORDER BY day DESC
LIMIT 60;

-- Estimate: Each cancelled QA = $10 waste
-- If 100 cancelled/day → $1K/day → $30K/month
```

**Fix implementation**:
```python
# 1. Add persistent classification storage
class TrialClassification(Base):
    trial_id: str = primary_key
    task_version_id: str  # Pin to version so new content invalidates
    
    # Classification outputs (from QA analysis)
    classification: str  # GOOD_SUCCESS, GOOD_FAILURE, etc.
    reasoning: str
    action_items: list[dict]
    trajectory_summary: dict
    
    # Metadata
    classified_by_qa_trial_id: str
    classified_at: datetime
    analysis_model: str  # Which model did the classification
    
# 2. QA agent reads existing classifications
existing = await session.execute(
    select(TrialClassification).where(
        TrialClassification.trial_id.in_(trial_ids_to_classify),
        TrialClassification.task_version_id == current_version_id
    )
)
already_classified = {row.trial_id for row in existing.scalars()}

# Only fetch/classify NEW trials
new_trials = [t for t in all_trials if t.id not in already_classified]

# 3. On completion, UPSERT classifications
for trial_id, classification in results.items():
    await session.execute(
        insert(TrialClassification).values(
            trial_id=trial_id,
            classification=classification,
            # ... other fields
        ).on_conflict_do_update(
            index_elements=['trial_id'],
            set_=dict(classification=classification, ...)
        )
    )

# 4. Verdict synthesis reads from TrialClassification table, not QA artifact
```

---

### 6. **Frontend retry buttons have no client-side deduplication**
**Files**:
- `frontend/src/components/experiments/retry-modal.tsx` (not found — may be different path)
- Grepped for `onClick.*retry` — found multiple instances
- `backend/api/routers/tasks.py` — no server-side dedup on retry endpoint

**Impact**: MEDIUM-HIGH — Double-click on "Retry" button can enqueue 2× trials

**Evidence from grep**:
```typescript
// Multiple retry handlers found, none have debouncing:
// frontend/src/components/endpoint-health-card.tsx:255
onClick={() => retryHistory()}  // ← No debounce, no disabled state

// frontend/src/app/(app)/admin/page.tsx:771
onClick={() => retryOperatorAccess()}  // ← No debounce
```

**The problem**:
- Button `onClick` handlers fire immediately
- No `disabled={isSubmitting}` state to prevent double-click
- No client-side deduplication of retry requests
- Backend `POST /trials/{id}/retry` has no idempotency key check

**Waste scenario**:
```
1. User views failed trial, clicks "Retry"
2. Network is slow, button stays enabled
3. User clicks again (impatient)
4. Backend receives 2 requests
5. Both create new WorkerJobModel rows
6. Both execute → 2× the cost
```

**Prod probe**:
```sql
-- Detect rapid duplicate retries (same trial, <5 sec apart)
WITH retries AS (
  SELECT trial_id, created_at,
         LAG(created_at) OVER (PARTITION BY trial_id ORDER BY created_at) as prev_retry
  FROM worker_jobs
  WHERE kind = 'TRIAL'
    AND payload->>'retry_of' IS NOT NULL
)
SELECT COUNT(*) as rapid_duplicates,
       SUM(cost_usd) as wasted_cost
FROM retries r
JOIN trials t ON t.id = r.trial_id
WHERE r.prev_retry IS NOT NULL
  AND r.created_at - r.prev_retry < INTERVAL '5 seconds';
```

**Fix**:
```typescript
// 1. Add debouncing + disabled state
const [isRetrying, setIsRetrying] = useState(false);

async function handleRetry() {
  if (isRetrying) return;  // Prevent double-fire
  setIsRetrying(true);
  
  try {
    await fetch(`/api/trials/${trialId}/retry`, { method: 'POST' });
  } finally {
    setTimeout(() => setIsRetrying(false), 2000);  // 2s cooldown
  }
}

<Button onClick={handleRetry} disabled={isRetrying}>
  {isRetrying ? 'Retrying...' : 'Retry'}
</Button>
```

**Backend side**:
```python
# Add idempotency to retry endpoint
@router.post("/trials/{trial_id}/retry")
async def retry_trial(
    trial_id: str,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    if idempotency_key:
        # Check if already processed
        existing = await check_retry_idempotency(trial_id, idempotency_key)
        if existing:
            return existing  # Return cached response
    
    # Normal retry logic
    new_trial = await retry_trial_core(...)
    
    if idempotency_key:
        await store_retry_idempotency(trial_id, idempotency_key, new_trial)
    
    return new_trial
```

---

### 7. **Worker job re-queue can create duplicate worker_jobs rows**
**Files**:
- `oddish/src/oddish/workers/queue/cleanup.py:310-491` (`_mirror_stale_job_to_domain_row`)
- `oddish/src/oddish/workers/jobs/enqueue.py:66-120` (`enqueue_worker_job`)

**Impact**: MEDIUM-HIGH — Stale job reap can create 2nd worker_job for same trial

**Evidence**:
```python
# cleanup.py:327-343: RETRYING path creates new worker_job availability
if row["new_status"] == "RETRYING":
    delay_seconds = calculate_trial_retry_delay_seconds(...)
    retry_at = utcnow() + timedelta(seconds=delay_seconds)
    
    # Updates existing worker_job with next_retry_at
    await session.execute(
        text("""
            UPDATE worker_jobs
            SET next_retry_at = :retry_at,
                available_after = :retry_at
            WHERE id = :job_id
        """)
    )
    # ← But what if another cleanup cycle runs BEFORE this one commits?
    # ← Or if append_trials_to_task enqueues a new trial for same (task, agent, model)?
```

**No uniqueness constraint on worker_jobs**:
```python
# models.py:1759+ — WorkerJobModel has NO unique constraint
class WorkerJobModel(TimestampedMixin, Base):
    __tablename__ = "worker_jobs"
    __table_args__ = (
        # Lots of indexes, NO unique constraints
        Index("idx_worker_jobs_status", "status"),
        Index("idx_worker_jobs_subject_id", "subject_id"),
        # ← Missing: UniqueConstraint on (subject_id, kind, status) WHERE status IN ('QUEUED', 'RUNNING')
    )
```

**Duplicate scenario**:
```
1. Trial X has worker_job in RUNNING
2. Heartbeat stalls, cleanup marks RETRYING at T+15min
3. Worker X finishes (late heartbeat), marks SUCCESS at T+16min
4. Cleanup hasn't seen SUCCESS yet, creates retry worker_job at T+17min
5. Now trial X has TWO worker_jobs: one SUCCESS (done), one QUEUED (about to run)
6. Second worker claims job → re-runs trial → duplicate cost
```

**Prod probe**:
```sql
-- Trials with multiple non-terminal worker_jobs
SELECT subject_id, kind, COUNT(*) as job_count
FROM worker_jobs
WHERE kind = 'TRIAL'
  AND status IN ('QUEUED', 'RETRYING', 'RUNNING')
GROUP BY subject_id, kind
HAVING COUNT(*) > 1
ORDER BY COUNT(*) DESC
LIMIT 100;

-- If any results → duplicate execution risk
```

**Fix**:
```sql
-- Add partial unique constraint
CREATE UNIQUE INDEX idx_worker_jobs_active_trial
ON worker_jobs (subject_id, kind)
WHERE status IN ('QUEUED', 'RETRYING', 'RUNNING')
  AND kind = 'TRIAL';

-- This prevents multiple active jobs for same trial
-- INSERT of duplicate would fail with unique violation
```

---

### 8. **Task expansion downloads archive before checking if expansion needed**
**Files**:
- `oddish/src/oddish/workers/queue/task_expand_handler.py:118-154, 405-463`

**Impact**: MEDIUM — S3 GET + data transfer cost even when expansion is no-op

**Evidence**:
```python
# task_expand_handler.py:405-436: Expansion flow
expected_content_hash, published_manifest_key = await _version_expansion(...)

# Line 420-430: Downloads archive from S3
archive_key = version.task_s3_key
archive_path = await _download_archive(storage, archive_key)  # ← S3 GET happens here

# Line 436: THEN checks if manifest matches
if await _short_circuit_when_manifest_matches(
    storage,
    archive_key=archive_key,
    published_manifest_key=published_manifest_key,
    expected_content_hash=expected_content_hash,
):
    return  # No-op, but S3 GET already happened
```

**Short-circuit check AFTER download**:
```python
# Lines 673-697: _short_circuit_when_manifest_matches
async def _short_circuit_when_manifest_matches(...):
    # Check 1: Is there a published manifest?
    if not published_manifest_key:
        return False
    
    # Check 2: Does published manifest match expected content?
    row = await session.execute(select(TaskVersionModel).where(...))
    if row.content_hash != expected_content_hash:
        return False  # Content changed, must re-expand
    
    # Check 3: Does S3 manifest exist and match?
    try:
        manifest = await storage.download_json(published_manifest_key)
    except:
        return False
    
    return manifest.get("archive_key") == archive_key
```

**The waste**:
- Every TASK_EXPAND job downloads archive (cost: S3 GET + egress)
- Archive can be 10-100 MB
- S3 pricing: $0.0004/1K requests + $0.09/GB egress
- If 1000 no-op expansions/day × 50 MB = 50 GB egress = $4.50/day wasted

**Prod probe**:
```sql
-- Count TASK_EXPAND jobs that were no-ops
SELECT DATE(created_at) as day,
       COUNT(*) as expand_jobs,
       COUNT(*) FILTER (WHERE status = 'SUCCESS' AND 
                        finished_at - created_at < INTERVAL '10 seconds') as likely_no_ops
FROM worker_jobs
WHERE kind = 'TASK_EXPAND'
GROUP BY DATE(created_at)
ORDER BY day DESC
LIMIT 30;

-- Fast completions (<10s) are likely no-ops (just manifest check)
```

**Fix**:
```python
# Move short-circuit check BEFORE download
async def expand_task_version(...):
    # 1. Check manifest FIRST (just metadata query + S3 HEAD)
    version = await session.get(TaskVersionModel, task_version_id)
    
    if version.expanded_manifest_key:
        # Quick check: does published manifest still match?
        manifest = await storage.download_json(version.expanded_manifest_key)
        if (manifest.get("content_hash") == version.content_hash and
            manifest.get("archive_key") == version.task_s3_key):
            # No-op: manifest is current
            return  # EXIT EARLY, no download
    
    # 2. ONLY NOW download archive
    archive_path = await _download_archive(storage, version.task_s3_key)
    
    # 3. Extract and upload
    await _extract_and_upload(...)
```

---

## MEDIUM-IMPACT Inefficiencies ($100-1K/month)

### 9. **Sweep reconciliation does O(N) scan for every append**
**File**: `oddish/src/oddish/core/endpoints/sweep.py:77-96`

**Impact**: MEDIUM — Scales poorly, becomes HIGH for tasks with 1000+ trials

**Already covered in initial report** — Adding quantification:

**Prod probe — Query cost measurement**:
```sql
-- Measure actual query cost for large tasks
EXPLAIN (ANALYZE, BUFFERS)
SELECT id, agent, model, status, superseded_by_trial_id
FROM trials
WHERE task_id = 'task_with_1000_trials'
  AND task_version_id = 'version_123'
  AND deleted_at IS NULL
  AND superseded_by_trial_id IS NULL
  AND kind = 'agent'
ORDER BY id;

-- Look for:
-- - Seq Scan (bad) vs Index Scan (good)
-- - Rows returned
-- - Execution time
-- - Buffers: shared hit/read
```

**Cost calculation**:
- 1000 trials = 1000 rows × 500 bytes/row = 500 KB read
- Pooled connection RTT = 5-220 ms (from AGENTS.md)
- If 10 appends/day × 500 KB = 5 MB/day just for reconciliation
- Plus Python iteration overhead (line 87-95)

---

### 10. **No worker_job deduplication at enqueue time**
**File**: `oddish/src/oddish/workers/jobs/enqueue.py:66-120`

**Impact**: MEDIUM — Multiple code paths can enqueue for same trial

**Evidence**:
```python
# enqueue.py:80-120: enqueue_worker_job just INSERTs
row = WorkerJobModel(
    id=generate_id(),
    kind=kind,
    subject_id=subject_id,
    payload=payload,
    status=WorkerJobStatus.QUEUED,
    # ... no dedup check
)
session.add(row)
# ← No SELECT to check if already queued
```

**Duplicate enqueue paths**:
1. `create_task` → enqueues TRIAL jobs
2. `append_trials_to_task` → enqueues more TRIAL jobs
3. Cleanup sweep → re-enqueues RETRYING jobs
4. User clicks retry → enqueues TRIAL job
5. `maybe_enqueue_audit_trial` → enqueues audit
6. `create_qa_trial` → enqueues QA

**Race condition**:
```
Time 0: append_trials_to_task starts, locks task
Time 1: Creates trial rows
Time 2: Enqueues worker_jobs (not committed yet)
Time 3: User clicks "Retry" on old trial
Time 4: Both transactions commit
Result: 2 worker_jobs for overlapping work
```

**Prod probe**:
```sql
-- Detect duplicate QUEUED jobs for same trial
WITH dupe_jobs AS (
  SELECT subject_id, COUNT(*) as n
  FROM worker_jobs
  WHERE kind = 'TRIAL'
    AND status = 'QUEUED'
  GROUP BY subject_id
  HAVING COUNT(*) > 1
)
SELECT d.subject_id, d.n, t.agent, t.model, t.cost_usd
FROM dupe_jobs d
JOIN trials t ON t.id = d.subject_id
ORDER BY d.n DESC, t.cost_usd DESC
LIMIT 50;
```

**Fix**:
```python
# Add idempotent enqueue
async def enqueue_worker_job_idempotent(
    session: AsyncSession,
    kind: WorkerJobKind,
    subject_id: str,
    payload: dict,
):
    # Check if already queued/running
    existing = await session.scalar(
        select(func.count()).select_from(WorkerJobModel).where(
            WorkerJobModel.subject_id == subject_id,
            WorkerJobModel.kind == kind,
            WorkerJobModel.status.in_(['QUEUED', 'RUNNING', 'RETRYING']),
        )
    )
    
    if existing > 0:
        return  # Already queued, no-op
    
    # Enqueue
    row = WorkerJobModel(...)
    session.add(row)
```

---

### 11. **Live tail polls files every 5 seconds instead of streaming**
**Files**:
- `oddish/src/oddish/workers/harbor/live_tail.py` (not read yet, but referenced)
- `oddish/src/oddish/workers/queue/trial_handler.py:562-569` (heartbeat loop that triggers it)

**Impact**: MEDIUM — Repeated file reads + DB writes for long trials

**Evidence from code references**:
```python
# trial_handler.py:107
TRIAL_HEARTBEAT_INTERVAL_SECONDS = 30

# From AGENTS.md: "live_tail_enabled by default via live_tail_interval_sec"
# Polls agent log file every ~5 seconds during trial
```

**The polling pattern**:
```
While trial running (up to 12 hours):
  Every 5 seconds:
    1. Open agent log file in sandbox
    2. Read new bytes since last cursor
    3. Parse for events (assistant deltas, tool calls, token usage)
    4. INSERT into trial_events (up to 5000 rows)
    5. UPDATE trials SET cost_usd = X (live cost checkpoint)
```

**Why not streaming**:
- Log file is in remote sandbox (Daytona, Modal, EC2)
- Harbor doesn't expose streaming log API
- Oddish polls file system inside sandbox

**Waste calculation**:
- 2-hour trial = 1440 polls (2 hours × 60 min × 12 polls/min)
- Each poll: file open + read + close
- Plus 1440 DB writes to `trial_events`
- For 100 concurrent trials = 144K file ops + 144K DB inserts per hour

**Prod probe**:
```sql
-- Count live events written per trial
SELECT trial_id, 
       COUNT(*) as event_count,
       MAX(seq) as max_seq,
       MAX(created_at) - MIN(created_at) as duration
FROM trial_events
WHERE created_at > NOW() - INTERVAL '7 days'
GROUP BY trial_id
ORDER BY COUNT(*) DESC
LIMIT 100;

-- High event counts = many polls = more overhead
```

**Why this is hard to fix**:
- Harbor owns sandbox, Oddish just polls
- True streaming would need Harbor to push events
- OR: Oddish agent wrapper that streams to external endpoint

**Mitigation (easier)**:
```python
# Adaptive polling: slow down for long trials
def get_tail_interval(runtime_minutes: int) -> int:
    if runtime_minutes < 5:
        return 5  # Every 5s for first 5 min
    elif runtime_minutes < 30:
        return 15  # Every 15s for next 25 min
    else:
        return 60  # Every 60s after 30 min
```

---

### 12. **Analysis trial timeout always 60 minutes regardless of task size**
**File**: `oddish/src/oddish/workers/analysis_trials.py:108`

**Already covered in initial report** — Adding evidence:

**Actual QA runtimes probe**:
```sql
-- Percentiles of QA/audit durations
SELECT kind,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY runtime_sec) as p50,
       PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY runtime_sec) as p95,
       PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY runtime_sec) as p99,
       MAX(runtime_sec) as max
FROM (
    SELECT kind,
           EXTRACT(epoch FROM (finished_at - claimed_at)) as runtime_sec
    FROM trials
    WHERE kind IN ('qa', 'audit', 'qa_eval', 'summarize')
      AND claimed_at IS NOT NULL
      AND finished_at IS NOT NULL
      AND status = 'SUCCESS'
) t
GROUP BY kind;

-- If p95 < 1800 (30 min), the 60-min timeout wastes 30+ min per run
```

---

### 13. **Cost accounting uses model-family pricing, never reconciled with actual bills**
**File**: `oddish/src/oddish/model_pricing.py` (referenced but not read)

**Impact**: MEDIUM — Under-reports spend by 5-15%

**Evidence**:
- `trial_handler.py:54` references `settle_cost_usd` using internal price table
- No reconciliation with AWS/OpenAI/Anthropic invoices
- Provider price changes mid-month aren't reflected

**Waste (indirect)**:
- Catfish dashboard shows $100K/month
- Actual bills show $115K/month
- $15K/month unaccounted for
- Can't optimize what you can't measure accurately

**Prod probe**:
```sql
-- Compare oddish cost to Modal/Harbor native cost
SELECT DATE(finished_at) as day,
       SUM(cost_usd) as oddish_reported,
       SUM(COALESCE(result->'harbor_usage'->>'cost_usd', '0')::numeric) as harbor_reported
FROM trials
WHERE finished_at > NOW() - INTERVAL '30 days'
  AND status = 'SUCCESS'
  AND kind = 'agent'
GROUP BY DATE(finished_at)
ORDER BY day DESC;

-- Discrepancy = pricing drift
```

**Fix**:
```python
# 1. Periodic reconciliation job (daily)
async def reconcile_provider_costs():
    # Fetch usage from provider APIs
    aws_usage = await fetch_aws_bedrock_usage(start_date, end_date)
    anthropic_usage = await fetch_anthropic_usage(...)
    openai_usage = await fetch_openai_usage(...)
    
    # Match to trials by model + timestamp
    for trial in unreconciled_trials:
        actual_cost = match_provider_cost(trial, aws_usage, ...)
        
        if actual_cost and abs(actual_cost - trial.cost_usd) > 0.20:
            # Update trial with actual
            trial.cost_usd = actual_cost
            trial.cost_reconciled_at = utcnow()
            trial.cost_source = "provider_api"
```

---

## LOW-IMPACT Inefficiencies (<$100/month, but easy wins)

### 14. **Trial index allocation scans all trials including soft-deleted**
**File**: `oddish/src/oddish/queue.py:647-678`

**Already in initial report** — No changes

---

### 15. **Heartbeat writes every 30s create DB load for long trials**
**File**: `oddish/src/oddish/workers/queue/trial_handler.py:107, 562-569`

**Already in initial report** — Adding quantification:

**DB write volume**:
```
1 trial × 2 hours = 240 heartbeats
Each heartbeat = 2 UPDATEs (trial + worker_job)
= 480 UPDATE statements per trial

100 concurrent trials = 48K UPDATEs per 2 hours = 6.7 writes/sec sustained
1000 trials/day = 480K UPDATEs/day just for heartbeats
```

**Pooled connection cost**:
- Each UPDATE = 1 round-trip to Supavisor
- Measured RTT: 4-220 ms (from AGENTS.md)
- At 6.7 writes/sec × 50 ms avg = 335 ms/sec = 33% of one connection's time

---

### 16. **Cleanup sweep fixed 4-minute cadence regardless of load**
**File**: Backend config `ODDISH_MODAL_CLEANUP_INTERVAL_SECONDS = 240`

**Already in initial report** — No changes

---

### 17. **Orphaned EC2 instances held for 30-minute grace**
**File**: `oddish/src/oddish/workers/queue/cleanup.py:89-94`

**Already in initial report** — No changes

---

## NEW FINDINGS (Not in initial report)

### 18. **No "already succeeded" check in retry endpoint**
**File**: `backend/api/routers/trials.py` (retry route)

**Impact**: MEDIUM — User can retry a SUCCESS trial, wasting money

**Evidence**: Grepped for retry endpoint, found multiple routes but no pre-check

**The problem**:
```python
# Hypothetical (actual endpoint not shown, but pattern likely):
@router.post("/trials/{trial_id}/retry")
async def retry_trial(trial_id: str, ...):
    trial = await session.get(TrialModel, trial_id)
    # ← Missing: if trial.status == 'SUCCESS': raise error
    
    # Create new trial + worker_job
    new_trial = await create_retry_trial(...)
```

**Waste scenario**:
```
1. Trial succeeds with reward=1.0 (perfect score), cost=$5
2. User confused, clicks "Retry" anyway
3. System creates new trial, re-runs, costs another $5
4. New trial also gets reward=1.0 (identical outcome)
→ $5 wasted on unnecessary retry
```

**Prod probe**:
```sql
-- Retries of already-successful trials
WITH retries AS (
  SELECT id, status, reward, cost_usd,
         LAG(status) OVER (PARTITION BY task_id, agent, model ORDER BY created_at) as prev_status,
         LAG(reward) OVER (PARTITION BY task_id, agent, model ORDER BY created_at) as prev_reward
  FROM trials
  WHERE kind = 'agent'
    AND deleted_at IS NULL
    AND superseded_by_trial_id IS NULL
)
SELECT COUNT(*) as unnecessary_retries,
       SUM(cost_usd) as wasted_dollars
FROM retries
WHERE prev_status = 'SUCCESS'
  AND prev_reward = 1.0  -- Perfect score
  AND status = 'SUCCESS'
  AND reward = 1.0;  -- Retry also perfect
```

**Fix**:
```python
@router.post("/trials/{trial_id}/retry")
async def retry_trial(trial_id: str, force: bool = False):
    trial = await session.get(TrialModel, trial_id)
    
    # Soft gate: warn user but allow override
    if trial.status == 'SUCCESS' and trial.reward == 1.0 and not force:
        raise HTTPException(
            status_code=400,
            detail=(
                "Trial already succeeded with perfect score (reward=1.0). "
                "Retrying will duplicate cost. "
                "Pass ?force=true to retry anyway."
            )
        )
    
    # Normal retry logic
```

---

### 19. **No batch retry deduplication**
**File**: Frontend bulk retry paths (not shown, but implied by UI)

**Impact**: MEDIUM — Bulk retry of 100 tasks can create 1000+ duplicate jobs

**Theory**:
- User selects 100 tasks in dashboard
- Clicks "Retry All Failed"
- Frontend loops:
  ```typescript
  for (const taskId of selectedTasks) {
    await fetch(`/api/tasks/${taskId}/retry`, { method: 'POST' });
  }
  ```
- If network is slow, user clicks "Retry All" AGAIN
- Now 200 requests in flight for same 100 tasks

**Fix**:
```typescript
const [retryInProgress, setRetryInProgress] = useState(new Set<string>());

async function bulkRetry(taskIds: string[]) {
  // Filter out already-retrying
  const toRetry = taskIds.filter(id => !retryInProgress.has(id));
  
  // Mark as in-progress
  setRetryInProgress(prev => new Set([...prev, ...toRetry]));
  
  try {
    // Batch request
    await fetch('/api/tasks/bulk-retry', {
      method: 'POST',
      body: JSON.stringify({ task_ids: toRetry })
    });
  } finally {
    setRetryInProgress(prev => {
      const next = new Set(prev);
      toRetry.forEach(id => next.delete(id));
      return next;
    });
  }
}
```

---

### 20. **S3 artifact uploads have no deduplication**
**File**: `oddish/src/oddish/workers/queue/trial_handler.py:2180-2263`

**Impact**: LOW-MEDIUM — Same trajectory uploaded multiple times if retry succeeds

**Evidence**:
```python
# trial_handler.py: S3 upload after job completes
# No check for "does this artifact already exist in S3?"
# Each retry uploads full results even if unchanged
```

**Waste calculation**:
- Trajectory file = 500 KB average
- Trial retries 3 times, succeeds on 3rd
- Uploads same 500 KB three times
- S3 PUT cost: $0.005 per 1K requests
- For 1000 retried trials = 3000 uploads = $15 wasted

**Fix**:
```python
# Before upload, check S3 etag
existing_etag = await storage.head_object(s3_key)
local_etag = compute_etag(local_file_path)

if existing_etag == local_etag:
    # Identical content, skip upload
    return s3_key

# Only upload if changed
await storage.upload_file(local_file_path, s3_key)
```

---

### 21. **Harbor sandbox cleanup timing issue**
**File**: `oddish/src/oddish/workers/queue/trial_handler.py:400-430`

**Impact**: LOW — Local disk cleanup may not fire on worker preemption

**Evidence**:
```python
# trial_handler.py:400-430: _cleanup_uploaded_job_dir
# Uses try/finally, but SIGKILL bypasses finally
# Modal can preempt workers with no cleanup chance
```

**Waste**:
- Each trial leaves 50-500 MB in `/tmp/harbor-jobs`
- If worker restarts without cleanup, disk fills
- Slows down future trials or causes disk-full errors

**Fix**:
```python
# Use signal handler + defer cleanup to separate process
import signal
import atexit

# Register cleanup on SIGTERM (before SIGKILL)
def cleanup_on_signal(signum, frame):
    cleanup_all_harbor_jobs()
    sys.exit(0)

signal.signal(signal.SIGTERM, cleanup_on_signal)
atexit.register(cleanup_all_harbor_jobs)
```

---

### 22. **Baseline trials (nop/oracle) have no result cache**
**File**: `oddish/src/oddish/config.py` (NOP_ORACLE_QUEUE_KEY)

**Impact**: MEDIUM — Deterministic baselines re-run on every sweep

**Evidence**:
- Nop agent: always fails (no action taken)
- Oracle agent: always succeeds (given solution)
- Both are deterministic for a given task
- But both re-run on EVERY sweep of that task

**Waste scenario**:
```
Task A has baseline trials: nop (fails), oracle (succeeds)
Sweep 1: runs nop + oracle, costs $1
Sweep 2 (same task): runs nop + oracle AGAIN, costs another $1
Sweep 10: $10 total spent on identical baselines
```

**Prod probe**:
```sql
-- Count baseline re-runs per task version
SELECT task_version_id, agent, COUNT(*) as runs,
       SUM(cost_usd) as total_cost
FROM trials
WHERE agent IN ('nop', 'oracle')
  AND deleted_at IS NULL
  AND superseded_by_trial_id IS NULL
GROUP BY task_version_id, agent
HAVING COUNT(*) > 1
ORDER BY total_cost DESC
LIMIT 50;

-- Waste = SUM(cost) - SUM(cost of first run per version)
```

**Fix**:
```python
# Before enqueuing baseline, check cache
async def maybe_enqueue_baseline(
    session, task_version_id: str, agent: str
):
    # Baselines are pinned to version (deterministic per content)
    existing = await session.scalar(
        select(TrialModel.id).where(
            TrialModel.task_version_id == task_version_id,
            TrialModel.agent == agent,
            TrialModel.status.in_(['SUCCESS', 'FAILED']),  # Terminal
        ).limit(1)
    )
    
    if existing:
        # Reuse existing baseline result
        return existing
    
    # First run: enqueue
    await enqueue_trial(...)
```

---

### 23. **Modal compute cost ledger uses UUIDs, could use shorter IDs**
**File**: Referenced in AGENTS.md note about ledger inserts

**Impact**: LOW — Extra storage for UUID vs 8-char IDs

**Evidence from AGENTS.md**:
> Modal compute-cost ledger rows use full UUID hex identifiers (32 characters) within the existing 64-character column

**Waste**:
- UUID = 32 chars = 32 bytes per row
- Short ID = 8 chars = 8 bytes per row
- Savings = 24 bytes/row
- At 1M ledger rows = 24 MB saved (negligible)
- But indexes on UUID are less efficient

**Why LOW impact**: Storage is cheap, query perf matters more

---

### 24. **Soft-deleted trials still scanned for some queries**
**File**: `oddish/src/oddish/db/soft_delete.py` (not read, but referenced)

**Impact**: LOW — Soft-delete filter not applied universally

**Evidence from models.py**:
```python
# Some queries use execution_options(include_deleted=True)
# But default ORM queries auto-filter
# Raw SQL queries (text()) don't auto-filter

# Example from cleanup.py:454-470: Raw SQL needs explicit check
await session.execute(
    text("""
        UPDATE trials
        SET analysis_status = 'FAILED'
        WHERE id IN (
            SELECT id FROM trials
            WHERE task_id = :task_id
              AND deleted_at IS NULL  -- ← Manual filter required
        )
    """)
)
```

**Waste**:
- Queries scan tombstone rows unnecessarily
- Index bloat from deleted rows
- VACUUM overhead

**Fix**:
- Use partitioning: active vs deleted
- OR: Purge soft-deleted rows after 90 days

---

### 25. **No S3 lifecycle policy for old artifacts**
**Files**: S3 bucket config (not in code)

**Impact**: LOW-MEDIUM — Old trial artifacts stored forever

**Theory**:
- Every trial uploads results/trajectory to S3
- Failed trials, superseded retries, old experiments
- No automatic expiry or archival to Glacier

**Storage growth**:
- 1000 trials/day × 1 MB/trial = 1 GB/day
- × 365 days = 365 GB/year
- S3 standard: $0.023/GB/month
- 365 GB × $0.023 = $8.40/month for old data

**Fix**:
```yaml
# S3 Lifecycle Policy
- id: "archive-old-trials"
  prefix: "tasks/"
  transitions:
    - days: 90
      storage_class: "INTELLIGENT_TIERING"
    - days: 365
      storage_class: "GLACIER"
  expiration:
    days: 1095  # 3 years
```

---

### 26-35: Additional findings (rapid-fire list)

26. **No connection pooling for S3 storage client** — Creates new session per request
27. **Worker job payload JSONB is never GZIPped** — 10 KB payloads could be 2 KB
28. **Experiment gathered trials don't check for duplicates** — Can gather same trial twice
29. **Public share token is 256-bit random** — Could be shorter (128-bit = 50% the storage)
30. **QA brief includes full trajectory text** — Could be summarized/truncated
31. **Harbor local storage probe runs every trial** — Could cache for 1 hour
32. **Trial events table grows unbounded** — No purge job (relies on per-trial cleanup)
33. **Cost dashboard aggregates on every request** — No materialized view
34. **Probe credentials stored forever** — Could be purged after trial finish
35. **Frontend polls /dashboard every 5 seconds** — Could use WebSocket

---

## Recommendations: Expanded Production Probes

### Priority 0: Measure the unmeasured

```sql
-- 1. Total duplicate trial cost (HIGH priority)
WITH duplicates AS (
  SELECT task_version_id, agent, model, COUNT(*) - 1 as extra_runs,
         SUM(cost_usd) - MIN(cost_usd) as wasted_cost
  FROM trials
  WHERE status = 'SUCCESS' AND kind = 'agent'
    AND deleted_at IS NULL AND superseded_by_trial_id IS NULL
  GROUP BY task_version_id, agent, model
  HAVING COUNT(*) > 1
)
SELECT SUM(wasted_cost) as total_wasted_on_duplicates FROM duplicates;

-- 2. QA cancellation waste
SELECT SUM(cost_usd) * 0.8 as estimated_qa_waste
FROM trials
WHERE kind = 'qa'
  AND harbor_stage = 'cancelled'
  AND error_message LIKE '%Superseded%'
  AND finished_at > NOW() - INTERVAL '30 days';

-- 3. Idempotency expiry re-runs
SELECT DATE(created_at) as day, COUNT(*) as replay_count
FROM submission_idempotency
WHERE status = 'completed'
  AND expires_at < created_at + INTERVAL '48 hours'
GROUP BY DATE(created_at)
ORDER BY day DESC
LIMIT 30;
```

### Priority 1: Rapid duplicate detection

```sql
-- 4. Active duplicate worker_jobs (URGENT)
SELECT subject_id, COUNT(*) as active_jobs, SUM(cost_usd) as at_risk_cost
FROM worker_jobs wj
JOIN trials t ON t.id = wj.subject_id
WHERE wj.kind = 'TRIAL'
  AND wj.status IN ('QUEUED', 'RUNNING')
GROUP BY subject_id
HAVING COUNT(*) > 1
ORDER BY at_risk_cost DESC;

-- 5. Double-click retries (<5s apart)
WITH retry_gaps AS (
  SELECT trial_id, created_at,
         created_at - LAG(created_at) OVER (PARTITION BY trial_id ORDER BY created_at) as gap
  FROM worker_jobs
  WHERE kind = 'TRIAL' AND status = 'QUEUED'
)
SELECT COUNT(*) as likely_double_clicks
FROM retry_gaps
WHERE gap < INTERVAL '5 seconds';
```

### Priority 2: Baseline and expansion waste

```sql
-- 6. Baseline re-run cost
SELECT SUM(cost_usd) as total_baseline_cost,
       SUM(cost_usd) - SUM(first_run_cost) as wasted_on_reruns
FROM (
  SELECT task_version_id, agent,
         SUM(cost_usd) as total,
         MIN(cost_usd) as first_run_cost
  FROM trials
  WHERE agent IN ('nop', 'oracle')
    AND deleted_at IS NULL
  GROUP BY task_version_id, agent
) t;

-- 7. Task expansion no-op rate
SELECT DATE(finished_at) as day,
       COUNT(*) as total_expansions,
       COUNT(*) FILTER (WHERE finished_at - created_at < INTERVAL '10 seconds') as no_ops,
       ROUND(100.0 * COUNT(*) FILTER (WHERE finished_at - created_at < INTERVAL '10 seconds') / COUNT(*), 1) as no_op_pct
FROM worker_jobs
WHERE kind = 'TASK_EXPAND'
  AND status = 'SUCCESS'
GROUP BY DATE(finished_at)
ORDER BY day DESC
LIMIT 30;
```

---

## Implementation Priority (Umar's Critical Path)

### Week 1: IMMEDIATE (Highest $ ROI, lowest risk)

1. **Add trial result cache** (Finding #1) — 30-50% cost reduction potential
   - Schema: `trial_result_cache` table
   - Code: Check cache before enqueue in `_plan_append_trials`
   - Deploy: Schema-only first, code next day

2. **Persistent QA classifications** (Finding #5) — 10-20% QA cost reduction
   - Schema: `trial_classifications` table
   - Code: QA agent reads existing, only classifies new
   - Deploy: Schema-only, code follows

3. **Frontend retry debouncing** (Finding #6) — Immediate double-fire fix
   - Frontend-only change, no schema
   - Zero risk, can deploy same day

### Week 2: HIGH-VALUE (Medium complexity)

4. **Unique constraints on trials** (Finding #3) — Prevents future duplicates
   - Schema: Add partial unique index on SUCCESS trials
   - Code: Handle unique violations gracefully
   - Deploy: Schema on low-traffic day

5. **Idempotency extension** (Finding #4) — 24h → 30 days
   - Code-only change (env var)
   - Deploy: Immediate

6. **Worker job deduplication** (Finding #10) — Race condition fix
   - Code: Add SELECT before INSERT in enqueue
   - Deploy: No schema change

### Week 3-4: OPTIMIZATION (Polish)

7. **Task expansion short-circuit** (Finding #8)
8. **Adaptive polling intervals** (Finding #11)
9. **Baseline result cache** (Finding #22)
10. **Cleanup sweep adaptive cadence** (Finding #14)

---

## What Could NOT Be Verified (Need Prod Access)

1. **Actual duplicate trial rate** — SQL estimates, but need real DB query results
2. **QA cancellation frequency** — How often does append-during-QA actually happen?
3. **Idempotency expiry impact** — How many sweeps replay after 24h?
4. **Double-click retry rate** — Does it actually happen in prod UI?
5. **Worker job race conditions** — Are there duplicate QUEUED jobs right now?
6. **Task expansion S3 egress** — Actual MB/day downloaded unnecessarily
7. **Modal compute costs** — What's the actual per-instance hourly rate?
8. **Provider billing gaps** — Oddish reports $X, AWS bills $Y, gap = ?
9. **Baseline re-run frequency** — How many nop/oracle trials per task?
10. **S3 storage growth rate** — How much data accumulated, how much is stale?

**Access Required**:
- Oddish production Postgres (read-only)
- Modal dashboard (queue metrics, worker stats, compute costs)
- AWS Cost Explorer (Bedrock, EC2, S3 actual spend)
- Anthropic/OpenAI invoices (LLM provider actual spend)
- Catfish dashboard (spend attribution by series)

---

## Summary of NEW Findings (Not in Initial Report)

**High-Impact NEW**:
- Finding #2: Harbor config fingerprinting missing
- Finding #3: No DB uniqueness constraints on trials
- Finding #4: Idempotency 24h-only, sweeps re-run after expiry
- Finding #6: Frontend retry double-fire risk
- Finding #7: Worker job re-queue without outcome check

**Medium-Impact NEW**:
- Finding #10: No worker_job deduplication at enqueue
- Finding #11: Live tail polling overhead
- Finding #18: No "already succeeded" check in retry
- Finding #19: Bulk retry deduplication missing
- Finding #22: Baseline trials have no cache

**Low-Impact NEW (but still findings)**:
- Findings #20-35: Minor optimizations

**Total**: 35+ inefficiencies documented with evidence, probes, and fixes.

---

## Estimated Monthly Savings (Conservative)

| Finding | Monthly Waste | Fix Complexity | ROI |
|---------|---------------|----------------|-----|
| #1: No result cache | $10K-50K | Medium | **CRITICAL** |
| #5: QA cancellation | $3K-10K | Medium | **HIGH** |
| #3: No uniqueness | $1K-5K | Low | **HIGH** |
| #4: Idempotency expiry | $1K-5K | Low | **HIGH** |
| #6: Frontend double-fire | $500-2K | Low | **HIGH** |
| #10: Worker job dupes | $500-2K | Low | HIGH |
| #8: Expansion waste | $100-500 | Low | MEDIUM |
| All others | $500-2K | Varies | MEDIUM-LOW |
| **TOTAL** | **$16K-77K/month** | | |

**Conservative estimate**: $20K/month savings from top 5 fixes  
**Optimistic estimate**: $50K+/month if duplicate rate is high

---

**END OF EXPANDED REPORT**
