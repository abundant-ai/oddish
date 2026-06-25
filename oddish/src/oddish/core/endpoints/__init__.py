"""Core task/trial/experiment endpoint logic, split across submodules.

This package was extracted from a single ``endpoints.py`` module. Everything
that used to live at ``oddish.core.endpoints`` is re-exported here so the
import surface (``from oddish.core.endpoints import ...``) is unchanged for
callers and tests. The implementation is grouped by concern:

- ``_common``: shared constants + fetch/reset helpers used across submodules
- ``tasks_query``: list / browse / status reads
- ``task_detail``: task versions + the detail rollup
- ``trials``: trial reads, retry, and log/result/trajectory IO
- ``qa``: task-level QA (analysis + verdict) run/cancel
- ``deletion``: soft-delete (task/experiment/trial) + combine
- ``sweep``: sweep expansion (single + batch)
"""

from __future__ import annotations

from oddish.core.endpoints._common import (
    USER_CANCELLED_MESSAGE,
    _primary_experiment_for_task_model,
    _reset_task_verdict,
    get_task_for_org_core,
    get_trial_for_org_core,
)
from oddish.core.endpoints.deletion import (
    _COMBINE_TRIAL_RESULT_FIELDS,
    combine_experiments_core,
    delete_experiment_core,
    delete_task_core,
    delete_trial_core,
)
from oddish.core.endpoints.qa import (
    backfill_task_analysis_core,
    cancel_task_qa_core,
    rerun_task_qa_core,
)
from oddish.core.endpoints.sweep import (
    build_task_sweep_response,
    create_task_sweep_batch_core,
    create_task_sweep_core,
)
from oddish.core.endpoints.task_detail import (
    _aggregate_task_detail_rollups,
    get_task_detail_core,
    get_task_version_core,
    list_task_versions_core,
)
from oddish.core.endpoints.tasks_query import (
    _build_browse_author_filter,
    _task_freetext_match,
    browse_tasks_core,
    get_task_status_core,
    list_tasks_core,
)
from oddish.core.endpoints.trials import (
    get_trial_by_index_core,
    get_trial_logs_core,
    get_trial_logs_structured_core,
    get_trial_result_core,
    get_trial_trajectory_core,
    retry_trial_core,
)

__all__ = [
    "USER_CANCELLED_MESSAGE",
    "_COMBINE_TRIAL_RESULT_FIELDS",
    "_aggregate_task_detail_rollups",
    "_build_browse_author_filter",
    "_primary_experiment_for_task_model",
    "_reset_task_verdict",
    "_task_freetext_match",
    "backfill_task_analysis_core",
    "browse_tasks_core",
    "build_task_sweep_response",
    "cancel_task_qa_core",
    "combine_experiments_core",
    "create_task_sweep_batch_core",
    "create_task_sweep_core",
    "delete_experiment_core",
    "delete_task_core",
    "delete_trial_core",
    "get_task_detail_core",
    "get_task_for_org_core",
    "get_task_status_core",
    "get_task_version_core",
    "get_trial_by_index_core",
    "get_trial_for_org_core",
    "get_trial_logs_core",
    "get_trial_logs_structured_core",
    "get_trial_result_core",
    "get_trial_trajectory_core",
    "list_task_versions_core",
    "list_tasks_core",
    "rerun_task_qa_core",
    "retry_trial_core",
]
