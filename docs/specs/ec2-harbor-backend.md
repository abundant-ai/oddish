# Ephemeral EC2 Harbor Backend

## Goal

Add EC2 as an opt-in Harbor compute provider using one public-IP, key-only SSH instance per trial. Modal remains the worker dispatcher and Daytona remains the default CPU backend.

## S1 — Provider lifecycle and routing

- Add opt-in EC2 settings, secure SSH-key materialization, an `Ec2Backend`, conditional registry wiring, CPU-only capability reporting, protected platform kwargs, ownership-tagged teardown, and Harbor lifecycle compatibility patching.
- Expose `environment=ec2` without changing default CPU/GPU/TPU routing.
- Reject attach mode, retained instances, accelerator requests, metadata-option overrides, and platform-owned EC2 kwarg overrides loudly. Allow only the optional platform-owned instance profile.
- Disable the instance metadata endpoint when no profile is configured. Require IMDSv2 tokens when the platform attaches a profile, and document that its permissions are exposed to sandbox code.
- Tests must cover disabled/enabled registration, unchanged defaults, config validation, key permissions, protected kwargs/tags, conditional IMDS behavior, optional platform profiles, lifecycle IDs, and tagged teardown.

## S2 — Harbor execution and hosted packaging

- Resolve and validate the complete provider environment config, including contextual trial and worker-job tags, once before choosing the normal or override-Harbor execution engine. Use it in-process and serialize it unchanged to the child; remove provider-specific child-payload merging.
- Install Harbor's EC2 dependencies and OpenSSH in worker environments.
- Add hosted policy, CLI/docs, environment examples, and deployment configuration. Keep the existing base runtime secrets unchanged; give the reconciler a dedicated EC2 control secret and give the default plus every blessed-variant trial worker that control secret plus a separate SSH-key secret. The API, dispatcher, and unrelated functions receive neither EC2 secret.
- Bake the deploy-time EC2 secret-name plan into immutable worker-image configuration, mirroring the GKE dependency-count guard; never derive Modal decorator dependencies from runtime-secret-injected flags.
- Tests must prove both execution engines receive the same resolved EC2 config and tags, the API/reconciler cannot see the SSH secret, default and variant workers receive it, deploy/container secret plans agree, and hosted acceptance/rejection behavior remains unchanged.

## S3 — Orphan reconciliation

- Extend `cleanup_orphaned_queue_state` with EC2 orphan target discovery: snapshot managed instances before opening the shared DB transaction, resolve liveness during the transaction, and add deletable instances as `("ec2", instance_id)` to the existing post-commit teardown targets.
- Preserve linked live instances and conservatively preserve unlinked instances with fresh running owners.
- Select terminal, missing-owner, or stale instances after 30 minutes and all managed instances after 14 hours.
- Scope discovery to protected managed/deployment tags and emit visible verdict logs/metrics. Never terminate directly from discovery; reuse the existing post-commit provider teardown so ownership verification and `TerminateInstances` have one implementation.
- Tests must cover fresh, linked, unlinked, terminal, missing, wrong-deployment, AWS-failure, hard-cap, and launch-then-startup-failure/cancellation cases.

## Acceptance

- No ECS, Auto Scaling group, infrastructure provisioning, GPU, private networking, or database migration.
- Existing targeted suites and the full affected Python suites pass.
- An AWS canary procedure is documented; live execution requires operator-provided AWS infrastructure and credentials.
