# GKE TPU Backend — Setup & Usage

This guide explains how to run oddish eval trials on **Google Kubernetes
Engine (GKE Autopilot)** — for TPU (v5e/v6e), GPU, or CPU workloads — starting
from zero knowledge of the backend.

## What this is, in one paragraph

Oddish runs each eval trial (an agent attempting a task, then a verifier
scoring it) inside an isolated sandbox. Historically those sandboxes are Modal
or Daytona containers. This backend adds a third option: run the trial as a
**pod on a GKE cluster**, which is what unlocks **TPU** hardware (Modal/Daytona
don't offer TPUs). It is designed to be **zero-touch**: you configure a few
coordinates once, and submitting a trial automatically creates the cluster,
namespace, and container image it needs, then deletes the cluster when it goes
idle. Nothing stands (or costs) when you're not running trials.

---

## 0. Single-tenant vs multi-tenant — read this first

These terms decide how much of the setup you actually need.

- A **single-tenant** deployment is used by **one trusted operator** running
  **trusted tasks** (tasks you authored). There is no untrusted code and no
  need to isolate one user's workload from another's. Example: you, on a
  preview, running your own migration tasks. Minimal setup: the config +
  IAM below is enough.
- A **multi-tenant** deployment is a **shared product** where **many
  independent organizations** submit trials through the same API, and each
  org's workload (often *agent-authored, untrusted* code) must be isolated
  from every other org's — network, identity, secrets, and blast radius.
  Example: the hosted oddish.app. This needs a real security boundary:
  per-tenant namespaces/identities, network policies, pod-security, secret
  scoping, and capacity fairness.

**Is oddish itself single- or multi-tenant?** The **product is multi-tenant** —
oddish.app serves many orgs off one deployment. But the **Modal and Daytona
backends are multi-tenant-*safe* for free**, because Modal/Daytona sandboxes
isolate each workload at the provider level: untrusted agent code in one
sandbox cannot reach another tenant's.

**Where does GKE stand?** GKE does **not** give that isolation automatically —
pods in a namespace share identity and network unless you configure otherwise.
So:
- Using GKE **single-tenant** (one operator, trusted tasks) — the setup in this
  doc is sufficient, and this is the mode proven so far.
- Exposing GKE to the **multi-tenant** oddish.app population (untrusted tasks)
  requires additional hardening **not yet built** (dedicated per-trial
  Kubernetes service accounts with no cloud IAM, default-deny network policies,
  restricted pod-security, scoped secrets, and an accelerator-capacity fairness
  layer). Treat multi-tenant GKE as a separate milestone; this guide gets you a
  correct single-tenant deployment.

---

## 1. Prerequisites (one-time, per GCP project)

You need a Google Cloud project and a few things enabled in it.

1. **APIs enabled:** Kubernetes Engine, Cloud Build, Artifact Registry, and (for
   TPU) Cloud TPU.
2. **An Artifact Registry Docker repo** to hold built task images (e.g. one
   named `oddish-envs` in your region). Images are pushed as
   `<region>-docker.pkg.dev/<project>/<repo>/<task-name>:<content-hash>`.
3. **A worker service account** the platform acts as. It needs these roles
   (this is the complete zero-touch set — trim if you disable a feature):

   | Role | Scope | Enables |
   |---|---|---|
   | `roles/container.clusterAdmin` | project | create/delete clusters on demand |
   | `roles/container.developer` | project | create pods, namespaces, read status |
   | `roles/iam.serviceAccountUser` | on the default compute SA `<num>-compute@developer.gserviceaccount.com` | cluster creation runs nodes *as* that SA — **easy to miss** |
   | `roles/cloudbuild.builds.editor` | project | build task images |
   | `roles/storage.admin` | on the `<project>_cloudbuild` bucket | stage build context |
   | `roles/artifactregistry.writer` | project | push built images |
   | `roles/storage.objectViewer` | project | pull build context |

   Grant pattern (run by a project owner; repeat `add-iam-policy-binding` per
   project-scoped role):
   ```bash
   SA=<worker-sa>@<project>.iam.gserviceaccount.com
   gcloud projects add-iam-policy-binding <project> \
     --member="serviceAccount:$SA" --role="roles/container.clusterAdmin" --condition=None
   gcloud iam service-accounts add-iam-policy-binding \
     <num>-compute@developer.gserviceaccount.com --project=<project> \
     --member="serviceAccount:$SA" --role="roles/iam.serviceAccountUser"
   gsutil iam ch "serviceAccount:$SA:roles/storage.admin" gs://<project>_cloudbuild
   ```
   > The `<project>_cloudbuild` bucket is created the first time any Cloud Build
   > runs; if absent, run one `gcloud builds submit` once.
4. **A Modal secret** (default name `oddish-gcp`) holding the worker SA key as
   `GOOGLE_APPLICATION_CREDENTIALS_JSON` (the full JSON, inline). It lives in
   Modal environment `main`; other environments read it from there. The worker
   materializes it to a file at runtime because Google SDKs discover
   credentials by file path.
5. **TPU quota** (only if running TPU), on the **Compute Engine** door
   (`compute.googleapis.com`, metric `preemptible_tpu_v6e` / `..._v5e`) — *not*
   the Cloud TPU API door. Flex-start zones known to serve today:
   - **v5e**: `us-west4-a`
   - **v6e**: `us-east5-a/b`, `asia-northeast1-b`

---

## 2. Configuration — the config file

All GKE settings are environment variables (prefix `ODDISH_`) read at deploy
time. In practice you put them in **`backend/.env`** (gitignored), which the
deploy loads automatically.

**Start from the committed template: [`backend/.env.gke.example`](../backend/.env.gke.example).**
Copy it to `backend/.env` and fill in the `<…>` placeholders:

```bash
cp backend/.env.gke.example backend/.env
# edit backend/.env: set PROJECT_ID, REGION, REGISTRY_LOCATION, REGISTRY_NAME
```

Only four values are required (project, region, registry location, registry
name); everything else has a safe default. Full reference:

| Var | Default | Meaning / when to change |
|---|---|---|
| `ODDISH_GKE_PROJECT_ID` | — | **Required.** GCP project; setting it registers the backend. |
| `ODDISH_GKE_REGION` | — | **Required.** e.g. `us-east5`. For TPU, a flex-zone region. |
| `ODDISH_GKE_REGISTRY_LOCATION` | — | **Required.** Artifact Registry region. |
| `ODDISH_GKE_REGISTRY_NAME` | — | **Required.** Artifact Registry repo name. |
| `ODDISH_GKE_CLUSTER_NAME` | derived | Defaults to `<MODAL_APP_NAME>-trials`. Set to pin a specific cluster. |
| `ODDISH_GKE_NAMESPACE` | `oddish-trials` | Pod namespace (auto-created). |
| `ODDISH_GKE_AUTO_PROVISION_CLUSTER` | `true` | Create cluster+namespace on demand. `false` requires a pre-existing cluster. |
| `ODDISH_GKE_IDLE_CLUSTER_TTL_HOURS` | `3` | Reap the created cluster after N idle hours. `0` disables the reaper. |
| `ODDISH_GKE_FLEX_START` | `true` | DWS flex-start for accelerator pods (CPU always standard). |
| `ODDISH_GKE_POD_READY_TIMEOUT_SEC` | `3600` | Max Pending wait (billing-free). Lower for CPU-only. |
| `ODDISH_GKE_AUTO_BUILD_MISSING_IMAGE` | `false` | Worker-side image-build fallback; enable on hosted deploys. |

> There is no separate YAML/TOML config file for the backend — the `.env`
> (env vars) is the whole configuration surface. Per-*submission* overrides are
> a different mechanism (see §4).

---

## 3. Deploy

From `backend/`, with `.env` populated:
```bash
MODAL_APP_NAME=<app> MODAL_ENVIRONMENT=<env> uv run modal deploy deploy.py
```
It prints either `GKE cluster '<name>' verified …` (auto-provision off) or
`… will be auto-provisioned on demand …` (auto-provision on).

> A CI deploy is GKE-less unless `.env` is present (it's gitignored, absent in
> CI), so a CI push reverts a manually GKE-enabled preview. Redeploy after CI
> settles, and never deploy while CI is mid-deploy (the app is stop-recreated,
> cancelling in-flight workers).

---

## 4. Running trials

**Three ways a submission routes to GKE** (any one):
1. The task's `task.toml` declares a TPU (auto-sniffed — only GKE serves TPUs):
   ```toml
   [environment.tpu]
   type = "v6e"
   topology = "2x2"   # 4 chips, single-host
   ```
2. Explicit `--env gke`.
3. `override_tpu` on the submission.

**CLI:**
```bash
oddish run <task-or-dataset> -a <agent> --env gke \
  -m <model> --n-trials <N> --github-user <u> --github-id <id>
```
Proven agents: `nop`, `oracle`, `claude-code`, `gemini-cli` (a valid model,
e.g. `gemini/gemini-3-flash-preview`).

**Per-submission overrides** (`--environment-kwarg KEY=VALUE`, JSON-typed) win
over the deploy defaults — route one run differently without redeploying:
```bash
oddish run <task> -a oracle --env gke \
  --environment-kwarg region=us-west4 \
  --environment-kwarg pod_ready_timeout_sec=900
```

---

## 5. What happens on a submission (zero-touch lifecycle)

1. **Image** — on upload, a Cloud Build produces the content-addressed image
   (worker-side fallback covers the race; concurrent builds converge to one).
2. **Cluster** — the first claiming worker creates the Autopilot cluster if
   missing (polls to RUNNING; concurrent workers are race-safe) and the namespace.
3. **Pod** — scheduled with flex-start (accelerators) or standard (CPU); Pending
   is billing-free; a permanent "Invalid Reservation" fails fast.
4. **Run → verify → teardown** — the verifier writes
   `/logs/verifier/metrics.json` (surfaced onto `trials.result`); the pod is deleted.
5. **Reap** — after the idle TTL with no GKE activity, the reaper deletes the
   platform-created cluster (only ones it labeled `harbor-managed`; hand-made
   clusters are never touched). Next submission recreates it.

---

## 6. Batch / manifest runs (the `/oddish` CI flow)

Batch runs go through the `/oddish` PR-comment workflow: a manifest lists
`agents:` and `tasks:`, and the CI runs `oddish run … -c sweep.yaml`. To make
that target GKE, three things must be true — in this order:

**Enablement sequence**

1. **The GKE routing must be on the CLI that CI installs.** The `/oddish` CI
   installs the oddish CLI from **`main`**, but GKE routing currently lives on
   the feature branch. So **merge the GKE PRs to `main`** (oddish + harbor-gke).
   This is low-risk: GKE is **inert without config** — with no
   `ODDISH_GKE_PROJECT_ID` the backend never registers and `--env gke` stays
   unavailable, so `modal`/`daytona` behavior is unchanged. Merging the *code*
   is safe; *enabling* it (next step) is the deliberate act.
2. **The target Oddish API must be deployed with GKE config.** CI's `oddish run`
   talks to a specific API; that deployment needs `backend/.env` with the
   `ODDISH_GKE_*` values (§2) and the `oddish-gcp` secret. **Tenancy caveat
   (§0):** until the multi-tenant hardening lands, point batch/CI at a
   **dedicated, single-tenant GKE-configured deployment** (trusted operator and
   tasks) — *not* the shared multi-tenant oddish.app.
3. **The manifest must be allowed to select GKE.** The `/oddish` parser
   historically allowlisted only `modal`/`daytona` for `metadata.environment`;
   **experiments PR #659** adds `gke`. Merge it to let a manifest set
   `metadata.environment: gke`. *(Not needed for TPU auto-sniff — see below.)*

**Two ways to actually route a batch (once the above is met)**

- **TPU tasks — auto-sniff (no manifest field, no CI change):** give each task a
  `task.toml [environment.tpu]` block; a normal manifest auto-routes it to
  GKE/TPU. TPU *type* is never a manifest field — it lives in the task.
  ```toml
  # tasks/<t>/task.toml
  [environment.tpu]
  type = "v6e"
  topology = "2x2"
  ```
- **Force the GKE backend (e.g. CPU tasks on GKE) — needs PR #659:**
  ```yaml
  # experiments/<name>/<name>.yaml
  metadata: {name: my-batch, task_path: tasks, oddish_only: true, environment: gke}
  agents: [{name: oracle, model_name: …, n_trials: 2}]
  tasks: [task-a, task-b]
  ```

**Direct CLI batch (bypasses CI, full control incl. v6e on CPU-defined tasks):**
```yaml
# sweep.yaml
environment: gke
agents: [{name: oracle, n_trials: 1}]
harbor: {environment: {override_tpu: {type: v6e, topology: "1x1"}}}
```
```bash
oddish run ./tasks -c sweep.yaml --environment-kwarg region=us-east5 \
  --github-user <u> --github-id <id>
```

> The **smithy** repo doesn't use oddish — its trials run through the separate
> **Taiga** pipeline, so there's no oddish/GKE path there.

---

## 7. Costs & hygiene

- Flex-start TPU bills a **fixed SKU only while a node is READY** (Pending is
  free). Approx v5e $0.60 / v6e $1.35 per chip-hour.
- The only idle cost is the Autopilot cluster-management fee (~$0.10/hr) — which
  the reaper eliminates by deleting idle clusters.
- Nothing stands when idle; everything materializes on submission.
