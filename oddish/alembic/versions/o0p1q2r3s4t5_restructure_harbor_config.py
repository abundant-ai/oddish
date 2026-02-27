"""restructure_harbor_config

Revision ID: o0p1q2r3s4t5
Revises: n9o0p1q2r3s4
Create Date: 2026-02-26 00:00:00.000000

Converts the flat harbor_config JSONB column on trials from the old
Oddish-specific key layout to the new structured format that mirrors
Harbor's native EnvironmentConfig / VerifierConfig / ArtifactConfig
Pydantic models.

Old format (flat):
    {"env_cpus": 4, "disable_verification": true, "agent_env": {...}, ...}

New format (nested):
    {
        "environment": {"override_cpus": 4, ...},
        "verifier": {"disable": true, ...},
        "artifacts": [...],
        "agent_overrides": {"env": {...}, "kwargs": {...}, ...}
    }
"""

from typing import Sequence, Union

from alembic import op


revision: str = "o0p1q2r3s4t5"
down_revision: Union[str, Sequence[str], None] = "n9o0p1q2r3s4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(r"""
        UPDATE trials
        SET harbor_config = (
            SELECT jsonb_strip_nulls(jsonb_build_object(
                'environment', jsonb_strip_nulls(jsonb_build_object(
                    'override_cpus',       harbor_config->'env_cpus',
                    'override_memory_mb',  harbor_config->'env_memory_mb',
                    'override_storage_mb', harbor_config->'env_storage_mb',
                    'override_gpus',       harbor_config->'env_gpus',
                    'force_build',         harbor_config->'force_build',
                    'kwargs', jsonb_strip_nulls(jsonb_build_object(
                        'gpu_types',                  harbor_config->'env_gpu_types',
                        'network_block_all',          CASE
                            WHEN harbor_config ? 'allow_internet'
                            THEN to_jsonb(NOT (harbor_config->>'allow_internet')::boolean)
                            ELSE NULL
                        END,
                        'sandbox_timeout_secs',       harbor_config->'sandbox_timeout_secs',
                        'sandbox_idle_timeout_secs',  harbor_config->'sandbox_idle_timeout_secs',
                        'auto_stop_interval_mins',    harbor_config->'auto_stop_interval_mins',
                        'auto_delete_interval_mins',  harbor_config->'auto_delete_interval_mins',
                        'snapshot_template_name',     harbor_config->'snapshot_template_name'
                    ))
                )),
                'verifier', jsonb_strip_nulls(jsonb_build_object(
                    'disable',             harbor_config->'disable_verification',
                    'override_timeout_sec', harbor_config->'verifier_timeout_sec'
                )),
                'artifacts',    harbor_config->'artifacts',
                'docker_image', harbor_config->'docker_image',
                'mcp_servers',  harbor_config->'mcp_servers',
                'agent_overrides', jsonb_strip_nulls(jsonb_build_object(
                    'env',                     harbor_config->'agent_env',
                    'kwargs',                  harbor_config->'agent_kwargs',
                    'override_timeout_sec',    harbor_config->'agent_timeout_sec',
                    'override_setup_timeout_sec', harbor_config->'agent_setup_timeout_sec'
                ))
            ))
        )
        WHERE harbor_config IS NOT NULL
          AND NOT harbor_config ? 'environment'
    """)


def downgrade() -> None:
    op.execute(r"""
        UPDATE trials
        SET harbor_config = (
            SELECT jsonb_strip_nulls(jsonb_build_object(
                'env_cpus',              harbor_config#>'{environment,override_cpus}',
                'env_memory_mb',         harbor_config#>'{environment,override_memory_mb}',
                'env_storage_mb',        harbor_config#>'{environment,override_storage_mb}',
                'env_gpus',              harbor_config#>'{environment,override_gpus}',
                'force_build',           harbor_config#>'{environment,force_build}',
                'env_gpu_types',         harbor_config#>'{environment,kwargs,gpu_types}',
                'allow_internet',        CASE
                    WHEN harbor_config#>'{environment,kwargs}' ? 'network_block_all'
                    THEN to_jsonb(NOT (harbor_config#>>'{environment,kwargs,network_block_all}')::boolean)
                    ELSE NULL
                END,
                'sandbox_timeout_secs',       harbor_config#>'{environment,kwargs,sandbox_timeout_secs}',
                'sandbox_idle_timeout_secs',  harbor_config#>'{environment,kwargs,sandbox_idle_timeout_secs}',
                'auto_stop_interval_mins',    harbor_config#>'{environment,kwargs,auto_stop_interval_mins}',
                'auto_delete_interval_mins',  harbor_config#>'{environment,kwargs,auto_delete_interval_mins}',
                'snapshot_template_name',     harbor_config#>'{environment,kwargs,snapshot_template_name}',
                'disable_verification',  harbor_config#>'{verifier,disable}',
                'verifier_timeout_sec',  harbor_config#>'{verifier,override_timeout_sec}',
                'artifacts',             harbor_config->'artifacts',
                'docker_image',          harbor_config->'docker_image',
                'mcp_servers',           harbor_config->'mcp_servers',
                'agent_env',             harbor_config#>'{agent_overrides,env}',
                'agent_kwargs',          harbor_config#>'{agent_overrides,kwargs}',
                'agent_timeout_sec',     harbor_config#>'{agent_overrides,override_timeout_sec}',
                'agent_setup_timeout_sec', harbor_config#>'{agent_overrides,override_setup_timeout_sec}'
            ))
        )
        WHERE harbor_config IS NOT NULL
          AND harbor_config ? 'environment'
    """)
