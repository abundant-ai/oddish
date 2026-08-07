# rewardkit-render-demo

A small RewardKit task for exercising oddish's reward rendering end to end.
Three programmatic dimensions plus the two standard `reward.toml` aggregates:

- `files` — three checks the reference solution passes.
- `completeness` — two checks the reference solution deliberately fails
  (`data/metrics.csv` has no data rows).
- `style` — one float-returning check that gives partial credit for report
  length.

Run the baselines and a real agent against it to see the three render states:

```bash
oddish run oddish/examples/rewardkit-render-demo -a oracle --n-trials 1
oddish run oddish/examples/rewardkit-render-demo -a nop --n-trials 1
oddish run oddish/examples/rewardkit-render-demo -a claude-code -m anthropic/claude-haiku-4-5 --n-trials 1
```

Expected scores: the oracle lands `{files: 1.0, completeness: 0.0,
style: 0.7, reward: 0.0, soft_score: 0.5667}` (verified against
harbor-rewardkit 0.1.7); `nop` zeroes everything; an agent that follows the
instruction fully earns `reward = 1.0`.
