<!-- promote-warning -->
> [!CAUTION]
> **DO NOT USE THE MERGE BUTTON ON THIS PULL REQUEST.**
> **THE BUTTON CREATES A NEW COMMIT AND BREAKS THE RELEASE MODEL.**
> **COMMENT `/promote` TO COMPLETE THE PROMOTION.**

<!-- promotion-target: REPLACE_WITH_STAGING_SHA -->
<!-- Replace the value above with the staging commit this release was
     validated against (git rev-parse origin/staging). Bare /promote promotes
     exactly that sha — commits that land on staging afterwards do not ride
     along — and /promote <sha> overrides it. An unfilled placeholder fails
     the promotion checks; deleting the whole line promotes the staging tip. -->

<!-- Keep the complete body under 300 words, including the release warning.
     Each included PR description must be under 50 words.
     Delete drafting instructions; retain promote-warning and promotion-target. -->

## TL;DR

<One sentence explaining what this release changes and why it matters.>

- **App code lines:** +<added> / −<removed>
- **Test code lines:** +<added> / −<removed>
- **Docs/other:** +<added> / −<removed>
- **Net:** <signed total> lines across <N> files

### What changed

- [#<number>](<PR URL>): <Explain this PR's effect in one or two plain
  sentences, under 50 words. Repeat for each included PR.>

### Tests

<State verification for the pinned commit, including Staging Deploy status
and relevant staging checks. Include material failures or coverage gaps.>

---
