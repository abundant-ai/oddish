from __future__ import annotations

from harbor.agents.installed.grok_build import GrokBuild

from .grok_build_trajectory import write_grok_build_trajectory_if_richer


class OddishGrokBuild(GrokBuild):
    """Grok Build wrapper that normalizes streamed output into ATIF."""

    def populate_context_post_run(self, context) -> None:
        super().populate_context_post_run(context)
        try:
            trajectory = write_grok_build_trajectory_if_richer(
                existing_trajectory_path=self.logs_dir / "trajectory.json",
                output_path=self.logs_dir / "grok-build.json",
                agent_version=getattr(self, "_version", None) or "unknown",
                model_name=self.model_name,
            )
        except Exception:
            self.logger.exception("Failed to write Grok Build trajectory fallback")
            return

        if trajectory and trajectory.final_metrics:
            metrics = trajectory.final_metrics
            context.n_input_tokens = metrics.total_prompt_tokens or 0
            context.n_cache_tokens = metrics.total_cached_tokens or 0
            context.n_output_tokens = metrics.total_completion_tokens or 0
            context.cost_usd = metrics.total_cost_usd
