---
name: write-pr
description: Write or rewrite PR titles and bodies from repository evidence for reviewers with no project context. Use when asked to draft, edit, create, open, or improve a pull request or its description.
---

# Write PRs

Explain the problem, resulting behavior, and relevant tests in plain sentences. Assume the reviewer has not seen the conversation. Select the facts they need to review the change; do not narrate the investigation.

## Length and readability

- Every PR description must be under 300 words, including headings, counts, and testing notes. This applies to feature, fix, and promotion PRs.
- In a promotion PR, each included PR's description must also be under 50 words. The whole promotion body still stays under 300 words.
- Aim for about 15 useful sentences for a substantive feature or fix, including the opening summary. Use fewer when the change is simple; never pad to reach a sentence count.
- Meet the word limit by omitting lower-value detail, not by packing sentences with abbreviations, jargon, semicolons, or file paths. Keep the causal explanation intact.

## Establish the facts

Read the diff against the actual PR base and the relevant test output. Compute exact additions and deletions. Distinguish observed results from expectations and author-reported claims. Never invent measurements or verification. Rewriting a description does not require rerunning tests.

Before changing a contract, check its readers, including installed clients and other packages. Mention compatibility in the explanation only when it matters to the review decision; do not add a separate compatibility inventory.

## Required body format

Start TL;DR with one plain sentence summarizing the change and its effect. Put the four line-count entries immediately below that sentence, before the explanation.

```markdown
## TL;DR

<One sentence explaining what changes and why it matters.>

- **App code lines:** +<added> / −<removed>
- **Test code lines:** +<added> / −<removed>
- **Docs/other:** +<added> / −<removed>
- **Net:** <signed total> lines across <N> files

### What changed

<Simple explanation of the previous behavior, its consequence, and the new behavior. Include a material compatibility or rollout limitation when needed.>

### Tests

<Simple explanation of the tests written or updated, what they prove, and observed results. State meaningful coverage gaps.>

---
```

Use valid Markdown with blank lines around headings, paragraphs, and lists. Write connected paragraphs under What changed and Tests. Use the supplied format instead of the old Scope/Size/Compatibility bullets and Core files changed inventory.

Count application behavior under App code lines, test cases and test fixtures under Test code lines, and documentation, test-runner configuration, other configuration, lockfiles, and generated text under Docs/other. Assign each changed line once. Net is all additions minus all deletions, not total churn. Count binary files in the file total without inventing line counts; identify them briefly if material. Keep zero-count categories so the four entries remain consistent.

## Choose what earns space

- Explain the trigger and consequence before the fix. Define unfamiliar project terms where they appear, or use a familiar phrase instead.
- Describe tests written or updated in terms of the behavior they verify. Give the strongest relevant results rather than every suite's tally. Distinguish real services from mocks where that limits the evidence.
- Preserve material failures, untested behavior, and required deployment actions. Call a failure pre-existing only after checking the base.
- Omit resolved setup failures, investigation chronology, repeated assurances, exhaustive test logs, and file inventories. The diff already shows which files changed. Name a file only when necessary to understand or act on the change.
- Include a screenshot or preview link when it materially demonstrates the behavior or the repository requires it. Do not create an empty screenshot section or routine “no UI changes” paragraph.

## Promotion PRs

Keep the same opening summary and line counts. Under What changed, link each included PR and explain its user-facing effect in one or two sentences, under 50 words per PR. Do not paste its original description or repeat its test history. Put shared release verification and limitations under Tests. Shorten individual entries as needed to keep the entire body under 300 words.

Preserve required machine-readable promotion markers, pinned commit IDs, and release instructions. These must remain accurate; do not use this writing skill to authorize merging or promoting a release.

## Before publishing

Count words in the complete body and, for promotions, each PR entry. Check that the first sentence is a useful summary, counts reconcile with the diff, and the explanation is readable without conversation context. Cut repetition before shortening sentences.

Make the title describe concrete behavior. Do not add co-author trailers or tool attribution unless explicitly requested. For a draft requested in chat, return Markdown in chat. Edit a remote PR only when authorized. With `gh`, save the exact body to a file, read it back, and use `--body-file` to preserve Markdown and newlines.
