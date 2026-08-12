"""Trajectory-summary component vocabulary (schema v5)."""

from __future__ import annotations

import enum


class TrajectoryBlockTaxonomy(str, enum.Enum):
    READING_FILES = "reading_files"
    THINKING_RECALL = "thinking_recall"
    THINKING_UNDERSTAND = "thinking_understand"
    THINKING_HYPOTHESIZE = "thinking_hypothesize"
    THINKING_CORRECTION = "thinking_correction"
    WRITING_PLAN = "writing_plan"
    IMPLEMENTING = "implementing"
    IMPLEMENTING_CORRECTION = "implementing_correction"
    WRITING_TESTS = "writing_tests"
    TESTING_PUBLIC = "testing_public"
    TESTING_CUSTOM = "testing_custom"
    TESTING_EDGE_CASES = "testing_edge_cases"
    DEBUGGING = "debugging"
