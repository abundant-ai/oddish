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

## Summary

<!-- One sentence: what this release ships. -->

## Commits in this promotion

<!-- Paste: git log --oneline origin/main..origin/staging -->

## Validation

<!-- Staging Deploy is green on the pinned target. Note anything soak-tested on staging.oddish.app. -->
