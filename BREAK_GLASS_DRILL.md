# Break-glass drill — do not merge without a decision

This file exists only to exercise the emergency path in a pull request that
targets `main` directly. Merging it moves `main` ahead of `staging`, breaks the
fast-forward rule on purpose, and deploys to production.

If this drill is run, repair immediately afterwards:

- `staging` has no unpromoted commits:
  `git push origin origin/main:staging`
- `staging` has unpromoted commits: rebuild it on the new `main`, then
  force-push `staging`.

Delete this file once the drill is complete.
