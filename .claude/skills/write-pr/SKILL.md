---
name: write-pr
description: Write or rewrite PR titles and bodies from repository evidence for reviewers with no project context. Use when asked to draft, edit, create, open, or improve a pull request or its description.
---

# Write PRs

Assume the reviewer has never worked in this codebase or seen the conversation. Explain the existing behavior, its consequence, and what changes before naming implementation files. Be concrete and succinct: supply missing context, not a transcript of the investigation.

## Establish the facts

Read the diff against the actual PR base, relevant commits and issues, and available test output. Use exact change counts. Distinguish your observations from author-reported results and expectations. Never invent causes, measurements, or verification. Rewriting a body does not require rerunning tests.

## Structure

Use this order unless the user requests another format. Retain required repository fields without repeating the explanation.

```markdown
## TL;DR

- Scope: <Affected behavior in ordinary words>. Changes span <N> files: <counts by purpose>.
- Size: app +<N>/-<N>; tests +<N>/-<N>; docs +<N>/-<N> lines.

<Existing situation and concrete problem. Define a new feature or necessary
project term before using it. Explain who does what and what happens.>

<What happens after this change, and why that fixes the stated problem.>

<Only when needed: another distinct behavior, an important limitation,
or a required deployment action.>

### Local testing

<What was exercised, the relevant environment, and observed results.
Include important failures or untested behavior.>

### Core files changed

- **`path/file.ext`** — <What this file does differently and which
  previously explained behavior it affects.>
```

Keep Scope and Size as the only TL;DR bullets. Put the explanation directly below them in connected paragraphs, without another summary or separate Problem/Fix/Tested bullets. A small change may need two sentences. A large PR needs enough explanation to cover its distinct behaviors, not one paragraph per file.

Count application code separately from tests, docs, migrations, configuration, generated files, and assets where present. Omit absent categories; report binary files without invented line counts. File counts cover the whole diff. The final file list is selective, usually two to five bullets.

## Language

- Start with an action and consequence the reader can picture. Explain the old behavior before the replacement. For a new feature, first say what it is and what someone uses it for.
- Define necessary unfamiliar terms in place: "workers—the processes that run tasks." Prefer familiar words over introducing terminology that then needs explanation.
- Do not compress facts into labels such as "membership scoping," "state ownership," or "lifecycle handling." Say which records are read, who changes them, and when. "Correctly," "efficiently," and "robust" do not explain behavior.
- Each sentence should add a fact the reviewer needs. Keep the cause-and-effect chain, but remove repeated summaries, development chronology, obvious implementation trivia, generic benefits, and speculative follow-ups. Do not pad the body to make a small change sound substantial.
- Keep function names, filenames, and detailed mechanisms in the file section unless they are needed to understand the behavior. A file list does not substitute for an explanation. Include exact values or code examples when they settle a real question; do not narrate every changed line.
- Use full sentences rather than compressed headlines. Short paragraphs should connect ideas. Use lists for genuinely separate items; avoid turning every sentence into a heading or bullet.
- Explain preserved behavior or deployment steps only when they matter to this change. Do not append routine assurances, warnings, or "no production deployment" to unrelated PRs.

Language example:

Bad: "Decouple provisioning from external identity resolution to reduce pool contention."

Better: "First login held a database connection while waiting for Clerk, the sign-in service. That request can take ten seconds, leaving fewer connections for other logins. Login now releases the connection before contacting Clerk."

The better version names the action, the wait, its consequence, and the change. Do not add a sentence saying it "improves reliability and performance"; that repeats the point without evidence.

## Example body

This example sets the level of detail and language. Its numbers and test results belong to that change; never copy them into another PR. The title is "Require approval before organizations can spend Abundant's money."

```markdown
## TL;DR

- Scope: Require Abundant approval before an organization can use the API or run tasks. Also fix failures during organization creation.
- Size: app +480/-296; tests +807/-362; docs +84/-4; configuration +2/-0 lines.

Previously, someone could sign up, create an organization, and run tasks
at Abundant's expense. Being an administrator of their own organization
was enough. Nobody at Abundant had to approve it.

The server now checks approval on API requests, including requests using
existing keys. Workers—the processes that run tasks—check before starting
and every 15 seconds during execution. Revoking approval therefore also
stops work already underway.

This also fixes first login when Clerk, the sign-in service, has not yet
notified Oddish about the new organization. Repeated notifications no
longer cause duplicate-record errors.

The database update approves only Abundant, Abundant CyberMasters, and
Oddish-onsite. Every other organization needs manual approval. Apply
the database update before deploying the application.

### Local testing

177 backend tests and 56 worker tests passed against disposable local
PostgreSQL databases. They cover approval, existing API keys, sign-in,
and stopping work after approval is revoked.

Five end-to-end tests passed using a local API server and command-line
client. The full backend suite had 31 failures; the unchanged base branch
had the same 31 failures. No production deployment was performed.

### Core files changed

- **`org_access.py` / `worker/org_access.py`** — Check approval when
  accepting requests and running tasks.
- **`auth/provisioning.py` / `clerk_webhooks.py`** — Create organization
  records during first login and handle repeated sign-in notifications.
- **`org_execution_001.py` / `provision_org.py`** — Approve the three
  existing organizations and provide commands to approve or revoke others.
```

## Testing and file details

Explain what verification covered, not just "tests pass." Name relevant commands or test groups and results without dumping logs. State when services were mocked or checks did not run. For benchmarks, give the workload, real versus synthetic data, what was timed, and how measurements were summarized. Do not turn local results into production claims. Report a failure as pre-existing only after checking the base branch.

End with files that help the reviewer locate the explained behavior. Describe each file's change in a sentence; "updates helpers" is insufficient. Use the shortest unambiguous path and group related files. Skip incidental formatting, lockfiles, and generated files unless they are the substance of the PR.

## Drafting and publishing

Make the title describe concrete behavior; follow the repository's required title conventions. When asked for an example, provide Markdown in chat. When asked to update a PR, preserve the approved wording and format. With `gh`, write multiline text to a file and use `--body-file`; read the saved body back to verify it. A request for wording suggestions does not authorize changing a remote PR.
