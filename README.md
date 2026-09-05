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
    <img alt="Python" src="https://img.shields.io/badge/python-3.13-blue.svg">
  </a>
  <a href="LICENSE">
    <img alt="License" src="https://img.shields.io/badge/License-PolyForm%20Noncommercial-blue.svg">
  </a>
</p>

# Oddish

> Run evals on [Harbor](https://github.com/laude-institute/harbor) tasks in the cloud.

Oddish extends Harbor with:

- Provider-aware queuing and automatic retries for LLM providers
- Real-time monitoring via dashboard or CLI
- Postgres-backed state and S3 storage for logs

Just replace `harbor run` with `oddish run`.

## Quick Start

### 1. Install

```bash
uv pip install oddish
```

#### Install latest development version

```bash
uv pip install "oddish @ git+https://github.com/abundant-ai/oddish.git#subdirectory=oddish"
```

### 2. Generate an API key [here](https://oddish.app/)

Sign in, select an organization, and create a key from the dashboard. Organization
members can create `read` or `tasks` keys; organization administrators can also
create `full` keys. An API key cannot be used to create another API key.

```bash
export ODDISH_API_KEY="ok_..."
```

### 3. Submit a job

```bash
# Run a single agent
oddish run -d terminal-bench@2.0 -a codex -m gpt-5.5 --n-trials 3
```

```bash
# Or sweep multiple agents
oddish run -d terminal-bench@2.0 -c job.yaml
```

Example [job.yaml](assets/light-run.yaml)


### 4. Monitor Progress

```bash
oddish status
```

## Documentation

- [CLI docs](DOCS.md)
- [CLI package quick start](oddish/README.md)
- [Web dashboard](frontend/README.md)
- [Cloud backend](backend/README.md)
- [Self-hosting](SELF_HOSTING.md)
- [Agents](AGENTS.md)

## License

[PolyForm Noncommercial 1.0.0](LICENSE).

Personal and other noncommercial use is allowed under this license. It also
permits use by the organizations listed in its Noncommercial Organizations
section. Commercial use outside those terms requires a separate license from
the rights holders. Contact [the maintainer](https://github.com/RishiDesai).

These terms apply to new work released under this license. They do not remove
rights already granted for code released under Apache 2.0. The prior license is
kept in [LICENSE-APACHE-2.0](LICENSE-APACHE-2.0). Third-party code keeps its own
license. This project is no longer offered as open source under an OSI-approved
license.
