"""Project a parsed Harbor task config into persistable metadata.

Pure functions only — no database, no I/O. task.toml is the sole authority
for these fields; human curation lives in DIRECT tag assignments instead.
"""

from __future__ import annotations

import re

from oddish.schemas import TaskMetadata

_SLUG_SEPARATORS = re.compile(r"[^a-z0-9]+")


def slugify(value: str, *, max_len: int | None = None) -> str:
    """Normalize a vocabulary value so separator drift collapses.

    'ml_training' and 'ml-training' and 'ML Training' all become 'ml-training'.
    ``max_len`` truncates (rather than rejects) values wider than a tag
    policy's name_max_len -- see derived_tag_pairs.
    """
    slug = _SLUG_SEPARATORS.sub("-", value.strip().lower()).strip("-")
    if max_len is not None and len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug


def project_task_config(config: dict) -> TaskMetadata:
    """Extract persistable fields from a parsed task.toml.

    Every stanza is optional; a task.toml missing a section yields None/[]
    rather than raising, because upload must not fail on incomplete metadata.
    """
    metadata = config.get("metadata") or {}
    environment = config.get("environment") or {}
    agent = config.get("agent") or {}
    verifier = config.get("verifier") or {}

    category_raw = metadata.get("category")
    category_slug = slugify(str(category_raw)) if category_raw else ""
    topic_tags = [
        slug
        for slug in (slugify(str(tag)) for tag in (metadata.get("tags") or []))
        if slug
    ]

    # ``allow_internet`` is a legacy field Harbor normalizes into
    # ``network_mode`` and then drops -- a parsed TaskConfig dict never has
    # it, so reading it here was a silent always-None no-op. network_mode is
    # the real, three-valued field ("no-network" / "public" / "allowlist");
    # allow_internet is kept only as a derived convenience boolean for the
    # "does this task have any egress" question (allowlist is partial egress
    # but still True for that purpose).
    network_mode = environment.get("network_mode")
    allow_internet = None if network_mode is None else network_mode != "no-network"

    return TaskMetadata(
        description=metadata.get("description"),
        # category can slugify to "" (e.g. "---"); category_raw keeps the
        # original string regardless, since it's drift-forensics evidence.
        category=category_slug or None,
        category_raw=str(category_raw) if category_raw else None,
        topic_tags=topic_tags,
        author_name=metadata.get("author_name"),
        author_email=metadata.get("author_email"),
        author_organization=metadata.get("author_organization"),
        expert_time_hours=metadata.get("expert_time_estimate_hours"),
        network_mode=network_mode,
        allow_internet=allow_internet,
        cpus=environment.get("cpus"),
        memory_mb=environment.get("memory_mb"),
        storage_mb=environment.get("storage_mb"),
        gpus=environment.get("gpus"),
        gpu_types=list(environment.get("gpu_types") or []),
        agent_timeout_sec=agent.get("timeout_sec"),
        verifier_timeout_sec=verifier.get("timeout_sec"),
        build_timeout_sec=environment.get("build_timeout_sec"),
    )
