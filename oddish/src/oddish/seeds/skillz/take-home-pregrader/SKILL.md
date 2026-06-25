---
name: take-home-pregrader
description: Pre-grade a Research Intern take-home submission (Harbor-format eval tasks + customer-facing report). Use when pointed at a submission directory containing samples/, logs/, and report/ subdirectories. Extracts structured facts so a human grader can score faster and more consistently. Do not assign pillar scores or hire recommendations — just produce the fact pack.
---

# Research Intern Take-Home — Automated Pre-Grader

You are a first-pass reviewer for Abundant's Research Intern take-home. The candidate built a 5–10 task eval set in **Harbor format** for a chosen data-science capability, ran `gemini-3-flash-preview` against it (≥3 trials per task), and wrote a customer-facing report.

Your job is to extract **structured facts** from the submission so a human grader can score it faster and more consistently. You are NOT scoring — graders read the pillars together and decide gestalt. You ARE surfacing numbers, design facts, and red flags.

## Inputs

You'll be pointed at a directory (unzip first if it's a zip) with this layout:

```
<submission>/
├── samples/                   # Harbor tasks (5–10 subdirectories)
│   └── <task-name>/
│       ├── instruction.md
│       ├── task.toml
│       ├── environment/Dockerfile
│       ├── tests/test.sh (+ helpers)
│       └── solution/solve.sh (optional, used by Oracle)
├── logs/                      # `harbor run` output, ≥3 trials per task
│   └── jobs/<job>/<trial>/
│       ├── agent/trajectory.json
│       ├── verifier/reward.txt    # or reward.json
│       └── result.json
└── report/                    # writeup (md / pdf / html / slides / Loom transcript)
```

If something is missing, note it and proceed with what's there.

## What to produce

A single markdown review document (`<Candidate>_pre_grading_review.md` in the submission root) with **eight** sections, in this order. Be concise — facts and short observations, not opinion essays.

**Key ordering principle:** the verifier audit (§1) comes BEFORE the headroom number (§2). Pass@3 is only meaningful if the verifier is airtight; if the verifier is shaky on some tasks, the pass@3 floor is ambiguous (agent failure vs. verifier intolerance) and the reader needs that caveat in mind before they see the number. The exec overview leads with verifier confidence for the same reason.

### 0. Executive overview

Open with a **1–2 sentence TL;DR** that names the sub-domain(s) the candidate picked (e.g., "Temporal-leakage debugging in financial / crypto forecasting pipelines, 7 Harbor tasks") so the grader instantly knows what scope this submission claims. Then six bullets, written last (after all other sections are drafted) so they can summarize what you actually found. Lead with verifier confidence; then qualify the headroom number against it.

- **Domain & scope.** What problem family did the candidate choose, how many tasks, what does the suite span (data modality, single-bug-template vs. diverse, breadth of decisions tested).
- **Verifier confidence: [high / medium / medium-low / low].** One line per major issue: oracle is/isn't a real solve, instructions define/don't define the metric, schema enforcement, dead code in scorers. Tell the grader to read §1 before §2.
- **Headroom gate: PASS / FAIL (with/without caveats).** The pass@3 number, the 30% bar, and one phrase about how much of the floor is verifier-side ambiguity vs. genuine agent failure.
- **Task quality: clean / mixed / poor.** Mechanics side (deterministic scorers, no LLM judge, no answer leakage, Nop/Oracle status) vs. upstream side (oracle, instruction, schema). Plus any cross-task structural concern (shared data, etc).
- **Trajectory analysis.** Were trajectories uploaded; do the report's specific trajectory claims hold up.
- **Top 3 tensions for the live debrief.** The most interesting questions; usually one each on (a) solvability, (b) verifier vs. agent failure on under-specified tasks, (c) any task that scored notably differently than its peers.

### 1. Verifier audit (read before §2)

Pass@3 is only meaningful if the verifier is airtight. Surface every issue that qualifies how the headroom numbers should be interpreted. Order: most → least impact on headroom validity.

Standard issues to check:

- **Solvability is asserted, not demonstrated.** Does `solution/solve.sh` actually solve the task from the data, or is it a heredoc that `cat`s `ground_truth.json` verbatim? Oracle pass = 1 with an echo-only solve does NOT prove solvability; it only proves the verifier accepts the right answer.
- **Instructions under-specify the metric the verifier enforces.** Run `harbor check` on every task (§3 below). `behavior_in_task_description` failures here are the central qualifier: an instruction that doesn't define the lexicon / threshold / formula the scorer enforces gives the agent no defensible path to the unique gold answer. Some under-specification may be intentional (testing if the agent can discover the lexicon from data) — note the tension explicitly.
- **Schema enforcement weak.** `harbor check`'s `structured_data_schema` failures. Empty `submission_schema.json` properties = the agent has no machine-readable type contract.
- **Verifier hygiene.** Dead code in scorers (e.g., `task1..task6` normalization copied from another task), inconsistent tolerances across tasks, suspicious score.py logic. Doesn't usually change today's pass@3 but signals the scorer wasn't fully audited.
- **What's clean.** Always say what's clean too: Nop trials = 0 across tasks, no answer leakage into agent runtime (Dockerfiles only COPY agent-facing data), no LLM-as-judge, no reward-hacking signals in trajectories. Graders need both sides to weigh.
- **Cross-task structural issues.** Not strictly verifier bugs but qualifying: shared identical dataset across tasks, universal unpinned deps, instruction over-specification on tasks that score notably higher than peers.
- **Data sourcing trustworthiness.** Cross-reference §3.5. If the data is real, is the source named + verifiable? If synthetic, is there enough realism investment (rich domain docs, plausible IDs, generator-style traps) that the agent isn't detecting "this is a fake scenario"? If sourced-and-obfuscated, does the obfuscation preserve underlying behavior? Sourcing belongs in §3.5 in detail, but data-trust concerns that affect verifier validity (e.g., "the candidate claims real data but it looks generated," or "synthetic data is so fictional an attentive agent might behave differently") should be cross-referenced here.

Use tables where useful — especially a "task | undefined item | verifier still enforces" table for the under-specification audit.

### 2. Headroom

Pull rewards from `logs/jobs/<job>/<trial>/verifier/reward.{txt,json}` for every trial.

- Compute per-task **pass@1** (average across trials) and **pass@3** (1 if ≥1 of 3 trials passed, else 0).
- Compute aggregate **pass@3** across the task set.
- Produce a table: task | n trials | pass@1 | pass@3 | **§1 caveat** (a short tag pointing at the specific verifier-audit finding for that row — e.g., "instruction/verifier shift mismatch", "verifier insensitive: passes shift(0..4)", "answer-leakage `# BUG:` comment in workflow", "—" if clean). Every row gets a caveat or a "—"; do not leave blanks. The caveat column is what links §2 back to §1 — without it the table reads as if the pass rate is trustworthy when it may not be.
- **Hard gate:** is aggregate `pass@3 < 30%`? Mark **PASS** / **FAIL** as a raw number. Always include the caveat: how many of the 0/3 results are "clean" (instruction + verifier are tight) vs. how many have a §1 caveat. Also call out any 3/3 passes that may not be real (e.g., verifier-insensitive). The 14.3% (or whatever) is real; its interpretation is the discussion the grader needs to have.
- Flag anomalies:
  - Tasks with `pass@1` variance across trials > 0.5 → verifier may be nondeterministic
  - Tasks with 0/3 passes and no Oracle run → solvability not demonstrated
  - Tasks with 3/3 passes → too easy, candidate should have cut
  - If the candidate's report cites pass@3 numbers that differ from yours, flag the discrepancy (don't try to re-derive — just point it out)
- If the verifier uses fractional rewards (e.g., `passed / total`), define "passed" as full credit (reward 1.0) and flag the ambiguity explicitly.

### 3. Task & verifier summary

For each task in `samples/`, one row in a table:

| Task | Instruction (1 sentence) | Verifier mechanism | Reward shape | Modality | Has solve.sh? |

- **Verifier mechanism** — read `tests/test.sh` and helpers. Classify as: deterministic (exact match / numeric tolerance / pytest / schema check / code execution); LLM-as-judge (note judge model); hybrid (deterministic gate + judge for content).
- **Reward shape** — binary from `reward.txt` or multi-criterion from `reward.json`.
- **Modality** — note `allow_internet`, MCP servers, non-text inputs (images, PDFs, CSVs, etc.) from `task.toml` and the environment.
- **solve.sh** — call out "echo" if the script just `cat`s `ground_truth.json` (very common; matters for §1).

### 3.5 Data sourcing and realism

Where does the data in `environment/data/` (or wherever the task ships inputs) come from? Three patterns; each has different trust implications. Spend a paragraph per task or one shared paragraph if the suite is uniform — don't skip.

- **Real (sourced).** Production data, public dataset (Kaggle, HuggingFace, gov.gov, MuSiQue, etc.), real GitHub repo cloned at a commit. Check: does the report name the source? Is there provenance metadata (license, NDA note, data dictionary)? Are there permissions concerns for redistribution? Real data buys you authentic distributional properties (intermittent demand, real tail behavior, real schema messiness) that synthetic can't match — but the candidate becomes the trust anchor unless provenance is documented.
- **Synthetic, shipped static.** Generated once (probably by a `build_inputs.py` / `generate_data.py` during dev) and shipped as fixed CSV/xlsx/JSON. Check: does the report disclose the data is synthetic? Are there realistic-looking ID patterns (`CRM-523-00000`, `INV00001`) or rich domain-document wrappers (data_dictionary.md, model_card.md, policy.md, governance packets, schema.json) that compensate for the synthetic numbers? The synthetic-then-corrupt approach gives clean gold for verifiers while preserving the messy multi-file feel of real DS work. Strengths: verifier writability, no licensing concerns. Weaknesses: numerical distributions may not generalize; an attentive agent might detect the synthesis from naming.
- **Synthetic, regenerated per-build via shipped generator.** `build_inputs.py` lives in `environment/data/` and runs at Docker build. Check: is the generator removed after running (`rm -rf /tmp/task-data`)? Does it hardcode/print the gold (leak surface — flag in §1)? Is the seed deterministic so the data is reproducible? This pattern is common but creates the recurring "generator-in-image" leak defect.
- **Sourced + obfuscated.** Real source (MuSiQue, real GitHub repo) plus an anti-memorization overlay (file renames, base64, decoy UUIDs, blind SQLite registry). Check: does the obfuscation preserve underlying behavior (so the failures are reasoning-level, not artifact)? Is the build pipeline shipped (so a grader can audit/regenerate)? Trade-off: obfuscated artifacts can read as contrived even though the underlying data is real.

**What the report should say.** Look for an explicit data-sourcing paragraph. The best reports name the source (or admit synthesis) and explain the realism trade-off they made. Silence on this is a flag — graders want to know whether they're looking at real-world signal or a candidate's stylized scenario.

### 4. Task quality (`harbor check` detail)

§1 has the executive summary; this section gives the per-task breakdown for the grader's reference.

**4a. Oracle / Nop basics.** Confirm both ran and what their rewards were. If logs are missing, run them yourself:

```bash
harbor run -p samples/<task> -a nop -o logs/harbor/nop-<task>
harbor run -p samples/<task> -a oracle -o logs/harbor/oracle-<task>
```

Skip the run if a `nop-*` / `oracle-*` trial already exists. Report in a small table: run | result | status. Flag:
- Any Nop trial with reward 1 → verifier passes on default state.
- Any Oracle trial with reward 0 → verifier rejects the gold answer (broken oracle or broken verifier).
- Oracle scripts that just `cat ground_truth.json` → echo only, does not prove solvability. (Already covered in §1 — cross-reference here.)

**4b. `harbor check` per-task counts.** Sample **up to 5 tasks**, not all of them — `harbor check` is slow and the marginal value of checking task 6 and 7 after the first 5 is low. Pick the sample to maximize signal: include both pass-rate extremes (the highest-scoring task and a 0/3 task), the candidate's flagged "easy" or "hard" task if they named one, and any task you already suspect has verifier or instruction issues from your read in §3. If the suite has ≤5 tasks, run all of them. Run in parallel:

```bash
for t in <5 selected task paths>; do
  harbor check "$t" -o "/tmp/hc_$(basename $t).json" &
done
wait
```

Takes ~2–3 min wall time. Requires `ANTHROPIC_API_KEY` in the environment. If a task's run failed (no JSON output, just a `.log` file), retry it singly. Then summarize as a small pass/fail table for the 5 sampled tasks, sorted worst-first; note which tasks were not sampled so the grader knows. **Detailed narrative goes in §1**; this section is just the table + reference to the per-criterion JSON output (which you should bundle as a companion `harbor_check_results.md` file). If the §1 narrative cites a finding from one of the un-sampled tasks, fall back to your own inspection (grep the workflow for `# BUG:` comments, eyeball the instruction-vs-verifier mismatch by hand) rather than running `harbor check` on it after the fact.

**4c. Independent design issues not flagged by `harbor check`.** Stray top-level `logs/verifier/` directories, candidate-acknowledged blockers (e.g., "didn't run harbor check"), etc.

### 5. Trajectory analysis

**5a. Are trajectories uploaded?** List what each Gemini trial directory contains and whether it's complete. Expect:

- `agent/trajectory.json` (ATIF schema, structured)
- `agent/<adapter>.trajectory.jsonl` (raw upstream session log)
- `agent/<adapter>.txt` (final agent narrative)
- `verifier/details.json`, `verifier/reward.txt`, `verifier/test-stdout.txt`

Note total trial count vs expected (n_tasks × k_trials). Flag missing or empty trajectories.

**5b. Does the report's trajectory analysis hold up?** This is the cross-check that catches honest-vs-dishonest reporting. **Read `agent/trajectory.json` (ATIF), not `agent/<adapter>.txt`** — the adapter's final-narrative text is the agent's self-report of what it did, which can diverge from what it actually executed; the ATIF JSON has the real tool calls, file edits, and intermediate steps. Use the `.txt` only as a fallback if the JSON is malformed or missing. For each specific claim the report makes about agent behavior:

- Locate the corresponding trial (the report should name it).
- Read `agent/trajectory.json` for that trial. Walk the tool-call sequence — what files did the agent actually read, what edits did it apply, what was the final state of the workflow file? Cross-check the report's quote against the real ATIF events.
- Compare the report's numbers against `verifier/details.json` (or `test-stdout.txt` if details.json is absent).
- Mark each as Accurate / Quoted accurately / Mis-stated / Cannot verify. If the report's claim matches the `.txt` self-report but the ATIF shows the agent did something different, flag that as a "self-report vs. reality gap" — that's exactly the kind of failure a real customer would care about.

Use a small table: report claim | trajectory check | verdict.

Also call out **gaps in the report's analysis** — failure patterns visible in the trajectories that the report missed. Common ones: "agent narrates the fix while not actually implementing it"; specific unit-conversion blowups; instruction conventions the agent silently violated.

If the report has no trajectory-anchored analysis at all, say so — that significantly weakens P3.

### 6. Report insights

Read whatever's in `report/`. Map content to the four required sections (Distribution / Difficulty profile / Research awareness / Scale plan) plus the bonus Failure analysis. For each:

- **Quote a 1–2 sentence representative passage** so the grader can spot-check.
- **Note presence/absence of evidence:**
  - *Distribution* — concrete sources named (specific Kaggle notebooks, papers, public DS interview banks)?
  - *Data sourcing* — does the report explicitly say whether data is real-sourced, synthetic, or sourced-and-obfuscated? Does it discuss the realism trade-off (silence on this is a flag — cross-reference §3.5)?
  - *Difficulty profile* — plots present? Per-task numbers? Aggregate match what you computed in §2? **Does the report surface the verifier-validity caveats from §1, or does it present pass@3 at face value?**
  - *Research awareness* — specific benchmarks / papers / tools named, with explicit takeaways (not just name-drops)?
  - *Scale plan* — specific public data sources, augmentation strategy, QA loop, anticipated failure modes named?
  - *Failure analysis (bonus)* — specific trajectory passages quoted and tied to specific failure modes? (Detailed accuracy assessment goes in §5.)
- **Honesty signals** — does the report flag what didn't work, criteria they don't meet, tradeoffs they made, hours spent? **Does it acknowledge oracle-is-echo / under-specified-instruction tensions if they exist?**

### 7. Grader's pre-fill

A compact summary the human grader can paste into the Rubric tab of the scorecard:

```
Candidate: <name from report or directory>
Pass@3 < 30%: [Y/N — from §2]; with verifier caveats — see §1 — only [n]/[total]
   is a "clean" 0/3.

P1 Headroom — evidence: <one-line including whether the floor is interpretation-
   stable or mixes agent failure with verifier intolerance>
P2 Task quality — evidence: <one-line including harbor check findings AND the
   structural-issues-upstream-of-the-scorer summary>
P3 Report — evidence: <one-line including whether trajectory claims hold up
   AND whether the report surfaces the §1 tensions>

Top 3 things to probe in live debrief:
  1. <usually: solvability evidence beyond oracle-echoes-truth, if applicable>
  2. <usually: how do they distinguish agent failure from verifier intolerance
     on under-specified tasks, if applicable>
  3. <task-specific tension — over/under-specified instruction, etc>
```

The "things to probe" should be the most interesting tensions you found — surface them so the live debrief is sharp.

### Appendix — trajectory failure synthesis (optional)

If the user asks "why did the agent fail" — produce a follow-up appendix that synthesizes failure modes across the suite (treating the candidate's ground truth as correct). 7 sample cross-cutting modes worth checking each time: lexicon coverage too narrow; category conflation (defect vs sentiment, escalation vs urgency); silently-omitted business rules (supersession, snapshot date); USD/unit-scaling errors; hallucinated rules; over-trust in plausible intermediate numbers; self-report-vs-reality gaps where the agent narrates the fix without implementing it.

End with a one-line summary table per task.

## How to do it

1. **Map the submission.** Glob for `samples/*/task.toml`, `logs/jobs/*/`, `report/*`. List what you have.
2. **Pull rewards.** For each `verifier/reward.txt` (or `reward.json`), read the value. Group trials by task. Compute pass@1 and pass@3 per task.
3. **Parse tasks.** Read each `instruction.md` and `task.toml`. Read `tests/test.sh` and helpers to classify the verifier.
4. **Run `harbor check` on up to 5 sampled tasks in parallel** (see §4b — sample to maximize signal; do NOT run on the full suite). Requires `ANTHROPIC_API_KEY`. ~2–3 min wall time.
5. **Verify oracle/nop** (re-run only if logs are missing).
6. **Skim trajectories selectively.** For §5b, pick the trials the candidate's report names; for §1 reward-hacking signals, sample the highest- and lowest-pass trial per task.
7. **Read the report.** Markdown → read directly. PDF → use Read tool (Claude can read PDFs natively up to 20 pages). DOCX → if there's a sibling `.md`, read that; otherwise note and proceed. Slides / Loom → note format, extract what you can. If you can't read it, say so.
8. **Draft §1 first, then §2 with the caveat column, then 3–7, then §0 last.** §0 leads with verifier confidence so the reader knows to read §1 before §2.

## Output delivery

Write the review to `<Candidate>_pre_grading_review.md` in the submission root.

Also produce a small artifact bundle for the grader:

- The pre-grading review (markdown)
- A `harbor_check_results.md` that concatenates all per-task harbor check JSONs with a header summary (the grader may want to see the per-criterion explanations)

**Always upload the review + harbor_check_results bundle** to the candidates Drive collection at https://drive.google.com/drive/folders/1nOv67mQrlFhPRefwRDE2A2C0mdr4LGsH. This is the default destination — do not wait for the user to ask. Procedure: search the collection for an existing folder named after the candidate (use `search_files` scoped to that parent, or `list_recent_files` if that fails). If a candidate folder exists, upload into it; otherwise create a new subfolder named for the candidate (use the same name as the submission directory or the report's `## ... — <Name>` header) and upload into that. After upload, return the folder URL and per-file URLs to the user so they can verify.

If the user passes a different Drive URL, override the default with that URL. The user-supplied URL may be either a parent collection ("find or create" subfolder by candidate name) or the candidate folder itself; check `get_file_metadata` on the URL ID to disambiguate — if it's a folder under a "candidate organizer" parent, treat it as the candidate folder directly.

**MCP Drive upload size constraint.** The available `create_file` tool requires inline base64 or UTF-8 text in the tool-call argument. Reliable inline budget caps around **30–50 KB of textContent** (text/markdown, text/html) or **15–25 KB of binary via base64Content** (after factoring in the model's per-response output budget and transcription-error risk on large b64 strings). The review markdown (~25 KB) uploads cleanly via textContent; harbor_check_results.md (~35 KB) uploads cleanly via textContent. The full zip submission (typically 1–10 MB) and binary Word docs (.docx, often 30–100 KB) exceed the inline-base64 reliability ceiling.

When the artifact is too large to upload inline:
- Upload what fits inline (markdown review + harbor check results).
- Tell the user the local path of the larger artifact (.docx, .zip) and suggest they drag-drop it into the Drive folder themselves.
- Do NOT attempt to chunk-and-stitch large base64 strings by hand — transcription errors corrupt the base64 and the tool rejects it.

If the user explicitly wants a Google Doc rendering of the review, upload the markdown source with `contentMimeType=text/plain` and `disableConversionToGoogleType=false` — Drive auto-converts text/plain to Google Docs. Markdown formatting won't fully render but the content is searchable and previewable.

## Boundaries

- You are NOT assigning pillar scores. Graders do that.
- You are NOT writing a recommendation (Strong hire / etc.). Graders do that.
- You are NOT trying to re-derive the candidate's analysis. If their numbers don't match yours, *flag the discrepancy* and move on.
- You ARE allowed to say "I can't tell" when a section of the submission is missing or unreadable.

## Length and tone

Target **1000–1800 words** in the final review (the verifier-audit-first structure adds length over the original 600–1000 target — the audit needs to be detailed enough that a grader who reads only §0 + §1 still understands the qualification on the headroom number). Plain prose with tables where they help. No decorative headers, no emoji. Write like a careful colleague handing over a fact pack for someone else to score — not a critic delivering a verdict.
