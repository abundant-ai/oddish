# QA model request routing (opt-in)

This branch adds a request gateway; it does not enable it in any deployment.
The existing job queue still prioritizes QA and enforces worker concurrency.
Each gateway call separately reserves provider request/token capacity. One
worker can make zero, one, or several concurrent model calls; worker counts are
never treated as API requests per minute.

```
Existing QA-priority queue -> existing bounded workers -> QA sandbox
                                                          |
                                                  each model request
                                                          |
                                     dedicated Modal qa_model_gateway
                                                          |
                                    atomic PostgreSQL request reservation
                                                          |
                                      main / HDO / actual Bedrock runtime
```

## Routing rule

`ROUTE_THRESHOLD = 0.65`. Prefer the conversation's recent provider while the
next request fits its 65% budget; otherwise select the least-loaded compatible
pool below that threshold. Initial conversations prefer the first configured
pool. At 65%, a pool receives no additional request that would increase its
most constrained budget beyond 65%. If every pool is full, return Anthropic's
429 error shape with `retry-after: 5`, letting the agent's existing retry logic
wait. No running response is moved or replayed. Persistent overload can still
exhaust the agent's retries; this is not an unbounded wait queue.

Load is the maximum of request/input/output fractions for direct Anthropic,
or requests/weighted-token fractions for Bedrock. It includes outstanding
reservations, not just completed calls. Until usage arrives, text/tool prompt
JSON UTF-8 bytes conservatively estimate input tokens, without assuming cache
hits, and `max_tokens` reserves output. This means routing can switch earlier
than a provider dashboard's measured 65%; 65% is the projected admission budget.
On complete responses, actual input + cache-write tokens and actual output
replace estimates. Cache reads are excluded. Completed usage remains charged
for 60 seconds after completion, conservatively covering tokens emitted late
in a stream. Interrupted/unknown responses retain their estimates until the
original 600-second request deadline plus 60 seconds. Expired rows cease to
count and are deleted on subsequent admission/inspection. Settlement is
idempotent. No database connection is held during provider network operations.

Anthropic remaining-capacity headers feed the next decision immediately, before
the response body finishes. Their latest observation lasts 60 seconds; calls
outstanding at or after that observation are also reserved conservatively.
HTTP 429/5xx put that pool in a bounded cooldown; invalid credentials or model
access (401/403/404) cool it down for five minutes and return 503 so a subsequent
client retry can choose another pool. This cannot prevent every provider 429:
provider burst/acceleration limits and unobserved competing traffic still apply.

## Deployment configuration

Run both normal migration stacks before deploying. Core migration
`qa_model_router_001` adds `model_request_pools` and `model_request_leases`.
Existing worker-token columns provide gateway authentication; analysis READ
keys remain read-only and cannot call the gateway.

1. Deploy the separate `qa_model_gateway` Modal function while routing is off.
   It uses the existing worker/API runtime secret attachments. Its webhook label
   is `<API_WEBHOOK_LABEL>-qa-model`. Obtain its URL from the deployment output;
   do not point workers at the dashboard API. Streams have separate Modal
   capacity (16 containers, up to 64 requests each) and a 600-second deadline.
2. Set `ODDISH_QA_MODEL_POOLS` in the gateway and worker runtime environment.
   It is JSON containing verified quotas and credential environment-variable
   names, never credential values. The following is the **main account only**,
   using the September 4 inspected quotas; verify current quotas before use:

   ```json
   [{
     "id": "main",
     "quota_group": "anthropic:abundant:sonnet-5",
     "provider": "anthropic",
     "model": "claude-sonnet-5",
     "key_env": "ANTHROPIC_API_KEY",
     "requests_per_minute": 20000,
     "input_tokens_per_minute": 10000000,
     "output_tokens_per_minute": 2000000,
     "external_load_fraction": 0.10
   }]
   ```

   `external_load_fraction` reserves a share for traffic bypassing the gateway;
   10% above is an illustrative operating reserve, not a measurement. Provider
   observations also account for visible outside use. Bedrock exposes no
   equivalent remaining-token headers here: its reserve must cover competing
   AWS callers, and aggregate AWS usage must be checked during rollout.
3. Add HDO only after verifying its independent organization quota. Its route
   uses `provider: anthropic` and `key_env: ANTHROPIC_HDO_API_KEY`; supply its
   actual quotas and quota group. Shared organizations must be represented by
   one pool. Reservations are keyed by `quota_group`, so renamed routes on
   another API replica still debit the same account budget. Duplicate quota groups or identical credential values are rejected.
4. Add Bedrock only after validating the worker account's model entitlement,
   quotas, region, and required beta features. Use `provider: bedrock`, the
   actual inference-profile model ID, `region`,
   `key_env: AWS_BEARER_TOKEN_BEDROCK`, `requests_per_minute`,
   `tokens_per_minute`, and the model's `output_multiplier`. This implementation
   uses Bedrock bearer-token authentication, not IAM/SigV4. Beta headers are
   forwarded as `anthropic_beta`; a Bedrock pool is ineligible unless its
   `supported_betas` contains every requested beta. No unsupported beta is
   silently dropped. Unknown HDO quotas and the AWS request allowance are not
   assigned invented defaults.
5. Set `ODDISH_QA_MODEL_GATEWAY_URL` to the dedicated HTTPS origin, without the
   `/qa-model` suffix. Set `ODDISH_QA_MODEL_ROUTING_ENABLED=1` for workers.
   Start with a small QA canary; inspect request attribution before bulk runs.

Raw platform provider keys stay in the gateway. Each routed sandbox receives a
random credential whose hash is stored on its owned worker attempt. The gateway
checks the live worker, expiration/revocation, organization, and QA trial shape
on every call. Retry minting replaces the old token. Terminal/cancel cleanup
revokes it; old attempts cannot revoke a newer attempt's token. Existing
credential redaction applies to the injected `ANTHROPIC_API_KEY`.

## Scope and rollout checks

The opt-in covers platform-funded Claude Code `qa`, `audit`, and `qa_eval`
trials using Sonnet 5, including nested model calls routed through the same
base URL. BYOK, other models/agents, ordinary trials, custom agent imports, and
ephemeral Harbor variants retain existing behavior. This first version supports
text and local tool messages. Images, documents, and audio are rejected before
provider admission because JSON byte size cannot bound their token cost; add a
provider token-count adapter before enabling it for those workloads. The
Messages `count_tokens` endpoint is proxied through a configured Anthropic pool
and charged as a request with zero generation tokens.

`GET /admin/qa-model-capacity` is operator-only and reports per-pool routing
load, active model requests, reserved/recent requests, cooldown, and admission
availability. `qa_model_admitted` and `qa_model_request` logs identify the pool
and worker job without logging prompts or credentials. These are routing
measurements, not organization-wide billing or exact container counts.

Validate real streaming/tool/caching behavior on each pool with its deployed
credential before enabling it. Then repeat the 200-job batch and compare QA
first-start delay, gateway request waiting/throttling, successful QA completions
per minute, provider utilization, and database admission latency. Synthetic
protocol and PostgreSQL tests do not establish actual provider entitlement or
production throughput.

The existing worker limits remain in force. Adaptive launcher throttling based
on gateway waiting time and an unbounded durable model-request queue are not
part of this change; do not remove the worker cap to launch every sandbox.

Rollback: disable new worker routing, keep the gateway and pool configuration
serving already-issued tokens until those attempts drain, then stop the gateway.
The worker feature flag does not invalidate live gateway credentials. Do not
downgrade the migration while routed attempts are still active.

## Local verification (September 4, 2026)

The affected suite reported `261 passed, 2 warnings`: model-capacity PostgreSQL
races, gateway wire protocols, Harbor runner/configuration, trial preparation
and credential cleanup, scoped job tokens, and BYOK regression tests. Tests used a
disposable PostgreSQL 16 container and mocked provider transports; they made
no paid provider calls.

Both complete migration chains upgraded a fresh local database successfully.
The new core migration also downgraded to `qa_dispatch_001` and upgraded again.
`git diff --check` passed. Ruff passed for changed files with the repository's
existing import-order `E402` exceptions excluded.
