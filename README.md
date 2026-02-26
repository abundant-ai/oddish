<p align="center">
  <a href="https://github.com/abundant-ai/oddish">
    <img src="assets/oddish_jump.gif" style="height: 10em" alt="Oddish" />
  </a>
</p>

<p align="center">
  <a href="https://pypi.org/project/oddish/">
    <img alt="PyPI" src="https://img.shields.io/pypi/v/oddish.svg">
  </a>
  <a href="https://www.python.org/downloads/">
    <img alt="Python" src="https://img.shields.io/badge/python-3.12+-blue.svg">
  </a>
  <a href="https://opensource.org/licenses/Apache-2.0">
    <img alt="License" src="https://img.shields.io/badge/License-Apache%202.0-blue.svg">
  </a>
</p>

# Oddish

> Run evals on [Harbor](https://github.com/laude-institute/harbor) tasks at scale with queuing, retries, and monitoring.

## Overview

Oddish extends Harbor with:

- Provider-aware queuing and automatic retries for LLM providers
- Real-time monitoring via dashboard or CLI
- Postgres-backed state plus S3 for artifacts

**Harbor compatibility:** replace `harbor run` with `oddish run`.

## Quick Start

### 1. Install

```bash
uv pip install oddish
```

### 2. Generate an Oddish API key

- API key generation is restricted during the beta. To request access, contact the [maintainer](https://github.com/RishiDesai).

```bash
export ODDISH_API_KEY="ok_..."
```

### 3. Submit a job

```bash
# Run a single agent
oddish run -d terminal-bench@2.0 -a codex -m gpt-5.2-codex --n-trials 3
```

```bash
# Or sweep multiple agents
oddish run -d terminal-bench@2.0 -c sweep.yaml
```

<details>
<summary>Example <a href="assets/sweep.yaml">sweep.yaml</a></summary>

```yaml
agents:
  - name: claude-code
    model_name: anthropic/claude-sonnet-4-5
    n_trials: 3
  - name: codex
    model_name: openai/gpt-5.2-codex
    n_trials: 3
  - name: gemini-cli
    model_name: google/gemini-3-flash-preview
    n_trials: 3
```

</details>

### 4. Monitor Progress

```bash
oddish status
```

## Commands

- `oddish run` — submit a job
- `oddish status` — monitor progress
- `oddish clean` — cleanup jobs

## Documentation

- [Technical documentation](AGENTS.md)
- [SELF_HOSTING.md](SELF_HOSTING.md).

## License

[Apache License 2.0](LICENSE)
