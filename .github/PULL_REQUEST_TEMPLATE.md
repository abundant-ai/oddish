<!-- Feature PR into staging. Fill every section; delete the comments.
     Body format is defined in .claude/skills/write-pr/SKILL.md; agents run
     that skill. Rules: CONTRIBUTING.md. Promotion PRs (staging -> main) use
     the release-promotion template instead and complete with a /promote
     comment, never the merge button. -->

## TL;DR

- Scope: <Affected behavior in ordinary words>. Changes span <N> files: <counts by purpose>.
- Size: app +<N>/-<N>; tests +<N>/-<N>; docs +<N>/-<N> lines. <!-- app additions must stay under 500 -->
- Compatibility: <nothing a client reads changes | older CLI keeps working because <reason> | breaks CLI < x.y.z; see below>

<Existing situation and concrete problem. Define a new feature or necessary
project term before using it. Explain who does what and what happens.>

<What happens after this change, and why that fixes the stated problem.>

### Screenshot or preview

<Required when a user can see the change: an image, or the preview link with
the page and state named. Otherwise write "No user-visible change.">

### Local testing

<What was exercised, the relevant environment, and observed results.
Include important failures or untested behavior.>

### Core files changed

- **`path/file.ext`** — <What this file does differently and which
  previously explained behavior it affects.>
