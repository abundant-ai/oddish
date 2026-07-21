from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harbor.models.task.config import TaskConfig

from oddish.preflight.models import Severity


def _config(task_dir: Path) -> TaskConfig:
    return TaskConfig.model_validate_toml((task_dir / "task.toml").read_text())


from oddish.preflight.checks import closed_internet

_OPEN_TOML = """\
version = "1.0"

[metadata]
description = "open task"

[environment]
network_mode = "public"
"""

_IMPLICIT_TOML = """\
version = "1.0"

[metadata]
description = "no network config at all"
"""

_CLOSED_TOML = """\
version = "1.0"

[metadata]
description = "closed task"

[environment]
network_mode = "no-network"
"""

# A separate verifier container that is public, over an otherwise fully closed
# task. The hand-rolled draft missed this entirely.
_VERIFIER_ENV_OPEN_TOML = """\
version = "1.0"

[metadata]
description = "closed everywhere except a separate verifier container"

[environment]
network_mode = "no-network"

[agent]
network_mode = "no-network"

[verifier]
network_mode = "no-network"

[verifier.environment]
docker_image = "ubuntu:22.04"
"""

# A per-step agent override that reopens the network on an otherwise closed task.
_STEP_OPEN_TOML = """\
version = "1.0"

[metadata]
description = "closed task with one step that reopens the agent network"

[environment]
network_mode = "no-network"

[agent]
network_mode = "no-network"

[verifier]
network_mode = "no-network"

[[steps]]
name = "s1"

[steps.agent]
network_mode = "public"
"""

_JUSTIFIED_TOML = """\
version = "1.0"

[metadata]
description = "open but justified"
open_internet_justification = "Needs live PyPI to resolve the dependency graph under test."

[environment]
network_mode = "public"
"""


def test_closed_internet_flags_explicit_public(make_task):
    task_dir = make_task(task_toml=_OPEN_TOML)
    findings = closed_internet.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert "[environment]" in findings[0].message


def test_closed_internet_flags_implicit_public_default(make_task):
    # Harbor defaults network_mode to public, so a task.toml that says nothing
    # about the network is fully open. This is the case that matters most.
    task_dir = make_task(task_toml=_IMPLICIT_TOML)
    findings = closed_internet.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR


def test_closed_internet_passes_no_network(make_task):
    task_dir = make_task(task_toml=_CLOSED_TOML)
    assert closed_internet.check(task_dir, _config(task_dir)) == []


def test_closed_internet_flags_a_public_separate_verifier_container(make_task):
    task_dir = make_task(task_toml=_VERIFIER_ENV_OPEN_TOML)
    findings = closed_internet.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert "verifier.environment" in findings[0].message


def test_closed_internet_flags_a_per_step_agent_override(make_task):
    task_dir = make_task(task_toml=_STEP_OPEN_TOML)
    findings = closed_internet.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert "s1" in findings[0].message


def test_closed_internet_passes_with_a_real_justification(make_task):
    task_dir = make_task(task_toml=_JUSTIFIED_TOML)
    assert closed_internet.check(task_dir, _config(task_dir)) == []


def test_closed_internet_rejects_a_short_justification(make_task):
    toml = _OPEN_TOML.replace(
        'description = "open task"',
        'description = "open task"\nopen_internet_justification = "TBD"',
    )
    task_dir = make_task(task_toml=toml)
    findings = closed_internet.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR


def test_closed_internet_rejects_a_non_string_justification(make_task):
    # A list whose repr is long must not sneak past as a "justification".
    toml = _OPEN_TOML.replace(
        'description = "open task"',
        'description = "open task"\nopen_internet_justification = ["a", "b", "c", "d", "e", "f"]',
    )
    task_dir = make_task(task_toml=toml)
    findings = closed_internet.check(task_dir, _config(task_dir))
    assert len(findings) == 1


def test_closed_internet_flags_an_open_agent_over_a_closed_baseline(make_task):
    toml = _CLOSED_TOML + '\n[agent]\nnetwork_mode = "public"\n'
    task_dir = make_task(task_toml=toml)
    findings = closed_internet.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert "[agent]" in findings[0].message


def test_closed_internet_passes_allowlist(make_task):
    toml = """\
version = "1.0"

[metadata]
description = "allowlisted"

[environment]
network_mode = "allowlist"
allowed_hosts = ["pypi.org"]
"""
    task_dir = make_task(task_toml=toml)
    assert closed_internet.check(task_dir, _config(task_dir)) == []


from oddish.preflight.checks import solution_format


def test_solution_format_flags_patch_files(make_task):
    task_dir = make_task(
        solve_sh="#!/bin/sh\ncp fix.py /app/\n",
        extra_files={"solution/fix.patch": "--- a\n+++ b\n"},
    )
    findings = solution_format.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert "fix.patch" in findings[0].message


def test_solution_format_flags_diff_files(make_task):
    task_dir = make_task(
        solve_sh="#!/bin/sh\n",
        extra_files={"solution/fix.diff": "--- a\n+++ b\n"},
    )
    findings = solution_format.check(task_dir, _config(task_dir))
    assert len(findings) == 1


@pytest.mark.parametrize(
    "cmd",
    ["git apply fix.patch", "patch -p1 < fix.patch", "git am fix.patch"],
)
def test_solution_format_flags_patch_application_in_solve_sh(make_task, cmd):
    task_dir = make_task(solve_sh=f"#!/bin/sh\n{cmd}\n")
    findings = solution_format.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert findings[0].line == 2


def test_solution_format_passes_a_readable_solution(make_task):
    task_dir = make_task(
        solve_sh="#!/bin/sh\ncp /solution/fix.py /app/fix.py\n",
        extra_files={"solution/fix.py": "print('fixed')\n"},
    )
    assert solution_format.check(task_dir, _config(task_dir)) == []


def test_solution_format_is_silent_without_a_solution_dir(make_task):
    task_dir = make_task()
    assert solution_format.check(task_dir, _config(task_dir)) == []


def test_solution_format_ignores_a_patch_mention_in_a_trailing_comment(make_task):
    # A comment mentioning "git apply" is not a patch application. This check has
    # no suppression grammar, so a false positive here would be un-suppressible.
    task_dir = make_task(
        solve_sh="#!/bin/sh\ncp fix.py /app/  # do NOT git apply the upstream patch\n"
    )
    assert solution_format.check(task_dir, _config(task_dir)) == []


from oddish.preflight.checks import anti_cheat_soundness


def test_anti_cheat_flags_import_scan_regex(make_task):
    task_dir = make_task(
        extra_files={"tests/test_cheat.py": 'import re\nBAD = re.compile(r"\\bimport\\s+stripe\\b")\n'}
    )
    findings = anti_cheat_soundness.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert findings[0].line == 2


def test_anti_cheat_flags_bare_word_library_regex(make_task):
    task_dir = make_task(
        extra_files={"tests/test_cheat.py": 'import re\nBAD = re.compile(r"\\bopenpyxl\\b")\n'}
    )
    findings = anti_cheat_soundness.check(task_dir, _config(task_dir))
    assert len(findings) == 1


def test_anti_cheat_allows_hostname_regex(make_task):
    # Slashes and dots mean this is a URL, not a library-identifier scan.
    task_dir = make_task(
        extra_files={"tests/test_ok.py": 'import re\nOK = re.compile(r"https?://api\\.stripe\\.com")\n'}
    )
    assert anti_cheat_soundness.check(task_dir, _config(task_dir)) == []


def test_anti_cheat_allows_scoring_tokens(make_task):
    task_dir = make_task(
        extra_files={"tests/test_ok.py": 'import re\nOK = re.compile(r"\\bpassed\\b")\n'}
    )
    assert anti_cheat_soundness.check(task_dir, _config(task_dir)) == []


def test_anti_cheat_respects_suppression_comment(make_task):
    task_dir = make_task(
        extra_files={
            "tests/test_ok.py": (
                'import re\n'
                'OK = re.compile(r"\\bopenpyxl\\b")  # anti-cheat-ok: spec forbids this exact library by name\n'
            )
        }
    )
    assert anti_cheat_soundness.check(task_dir, _config(task_dir)) == []


def test_anti_cheat_ignores_non_test_files(make_task):
    task_dir = make_task(
        extra_files={"environment/app.py": 'import re\nX = re.compile(r"\\bopenpyxl\\b")\n'}
    )
    assert anti_cheat_soundness.check(task_dir, _config(task_dir)) == []


from oddish.preflight.checks import provenance


@pytest.mark.parametrize(
    "line",
    [
        "RUN git clone https://github.com/foo/bar /src",
        "RUN git lfs clone https://github.com/foo/bar",
        "RUN git fetch origin main",
        "RUN pip install git+https://github.com/foo/bar@abc123",
        "RUN npm install git+ssh://git@github.com/foo/bar",
        "RUN curl -L https://github.com/foo/bar/archive/main.tar.gz | tar xz",
        "RUN curl -L https://github.com/foo/bar/tarball/main | tar xz",
        "RUN wget https://codeload.github.com/foo/bar/tar.gz/main",
    ],
)
def test_provenance_flags_repo_fetches_in_dockerfile(make_task, line):
    task_dir = make_task(dockerfile=f"FROM ubuntu:24.04\n{line}\n")
    findings = [f for f in provenance.check(task_dir, _config(task_dir)) if f.line == 2]
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert findings[0].line == 2


def test_provenance_flags_fetch_in_an_environment_build_script(make_task):
    # A git clone in a script under environment/ runs at build time (the
    # Dockerfile invokes it) and bakes into the agent's image, exactly like a
    # direct RUN git clone.
    task_dir = make_task(
        extra_files={"environment/setup.sh": "#!/bin/sh\ngit clone https://github.com/foo/bar\n"}
    )
    findings = [f for f in provenance.check(task_dir, _config(task_dir)) if f.line == 2]
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR


@pytest.mark.parametrize("rel", ["solution/solve.sh", "tests/test.sh"])
def test_provenance_ignores_fetch_outside_the_build_context(make_task, rel):
    # solve.sh (oracle) and test.sh (verifier) run in phases the agent never
    # sees, so a fetch there cannot leak branch history to the agent.
    task_dir = make_task(
        extra_files={
            rel: "#!/bin/sh\ngit clone https://github.com/foo/bar\n",
            "environment/.dockerignore": "**/.git\n",
        }
    )
    assert provenance.check(task_dir, _config(task_dir)) == []


def test_provenance_ignores_a_fetch_in_a_trailing_comment(make_task):
    # A comment merely mentioning a fetch is not a fetch. The `#` here is a
    # trailing comment, not a suppression.
    task_dir = make_task(
        dockerfile="FROM ubuntu:24.04\nRUN echo done  # remember: never git clone the upstream\n",
        extra_files={"environment/.dockerignore": ".git\n"},
    )
    assert provenance.check(task_dir, _config(task_dir)) == []


def test_provenance_accepts_a_valid_suppression(make_task):
    task_dir = make_task(
        dockerfile=(
            "FROM ubuntu:24.04\n"
            "RUN git clone https://github.com/foo/dep  # provenance-ok: pinned third-party dep, not the task upstream\n"
        ),
        extra_files={"environment/.dockerignore": ".git\n"},
    )
    assert provenance.check(task_dir, _config(task_dir)) == []


def test_provenance_rejects_a_too_short_suppression_reason(make_task):
    task_dir = make_task(
        dockerfile=(
            "FROM ubuntu:24.04\n"
            "RUN git clone https://github.com/foo/dep  # provenance-ok: dep\n"
        ),
        extra_files={"environment/.dockerignore": ".git\n"},
    )
    findings = provenance.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert "at least 10 characters" in findings[0].fix_hint


def test_provenance_rejects_a_suppression_with_no_reason(make_task):
    task_dir = make_task(
        dockerfile="FROM ubuntu:24.04\nRUN git clone https://x/y  # provenance-ok:\n",
        extra_files={"environment/.dockerignore": ".git\n"},
    )
    assert len(provenance.check(task_dir, _config(task_dir))) == 1


def test_provenance_ignores_whole_line_comments(make_task):
    task_dir = make_task(
        dockerfile="FROM ubuntu:24.04\n# RUN git clone https://github.com/foo/bar\n",
        extra_files={"environment/.dockerignore": ".git\n"},
    )
    assert provenance.check(task_dir, _config(task_dir)) == []


def test_provenance_flags_a_git_dir_inside_the_build_context(make_task):
    task_dir = make_task(extra_files={"environment/repo/.git/HEAD": "ref: refs/heads/main\n"})
    findings = [f for f in provenance.check(task_dir, _config(task_dir)) if ".git" in f.message]
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR


def test_provenance_accepts_a_git_dir_excluded_by_context_dockerignore(make_task):
    # A .dockerignore at the build-context root (environment/) is the one Docker
    # actually reads, so it correctly suppresses the .git finding.
    task_dir = make_task(
        extra_files={
            "environment/repo/.git/HEAD": "ref: refs/heads/main\n",
            "environment/.dockerignore": "**/.git\n",
        }
    )
    assert provenance.check(task_dir, _config(task_dir)) == []


def test_provenance_flags_git_when_dockerignore_is_at_the_task_root(make_task):
    # Regression: Docker reads .dockerignore from environment/, NOT the task
    # root. A root .dockerignore does not protect the real build, so a .git in
    # environment/ must still be flagged despite it — otherwise the check gives
    # false confidence on a task that genuinely leaks.
    task_dir = make_task(
        extra_files={
            "environment/repo/.git/HEAD": "ref: refs/heads/main\n",
            ".dockerignore": "**/.git\n",  # wrong location, ignored by Docker
        }
    )
    findings = [f for f in provenance.check(task_dir, _config(task_dir)) if ".git" in f.message]
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR


@pytest.mark.parametrize(
    "git_file",
    ["solution/repo/.git/HEAD", "tests/repo/.git/HEAD", ".git/HEAD"],
)
def test_provenance_ignores_a_git_dir_outside_the_build_context(make_task, git_file):
    # Only environment/ is the Docker build context; Docker refuses paths
    # outside it, so a .git here provably cannot reach the agent's image.
    task_dir = make_task(
        extra_files={git_file: "ref: refs/heads/main\n", "environment/.dockerignore": "**/.git\n"}
    )
    assert provenance.check(task_dir, _config(task_dir)) == []


def test_provenance_warns_when_no_dockerignore_exists(make_task):
    task_dir = make_task()
    findings = provenance.check(task_dir, _config(task_dir))
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARN


def test_provenance_is_clean_with_a_context_dockerignore_and_no_git(make_task):
    task_dir = make_task(extra_files={"environment/.dockerignore": ".git/\n"})
    assert provenance.check(task_dir, _config(task_dir)) == []
