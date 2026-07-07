if (
  process.env.NEXT_PUBLIC_LOGFIRE_ENABLED === "true" &&
  process.env.NEXT_PUBLIC_API_URL
) {
  void import("@/lib/observability")
    .then(({ ensureLogfireConfigured }) => ensureLogfireConfigured())
    .catch((error) => {
      console.warn("Logfire browser configure failed", error);
    });
}
