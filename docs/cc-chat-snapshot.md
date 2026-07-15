# Pre-baked Daytona snapshots for claude-code

## The analyzer shares this snapshot

The sandbox-per-cohort analyzer provisions Daytona sandboxes with the same
claude-code requirement, so it reuses this snapshot rather than building its
own. It resolves `ODDISH_AGENT_DAYTONA_SNAPSHOT` first and falls back to
`ODDISH_CC_CHAT_DAYTONA_SNAPSHOT`, so setting only the cc_chat var (as prod
does today) covers both.

The analyzer agent has no use for harbor, but a leaner analyzer-only image
would not help: `install()` checks claude-code and harbor independently, so it
would still attempt harbor's `pip install` on every sandbox. One shared image
keeps `install()` down to two existence checks for both callers.
