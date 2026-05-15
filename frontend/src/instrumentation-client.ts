// Loaded by Next.js before React mounts. Keep the entrypoint small so browser
// tracing does not inflate the critical client bundle when it is disabled.

if (
  process.env.NEXT_PUBLIC_LOGFIRE_ENABLED !== "false" &&
  process.env.NEXT_PUBLIC_API_URL
) {
  void import("@/lib/observability")
    .then(({ ensureLogfireConfigured }) => ensureLogfireConfigured())
    .catch((error) => {
      console.warn("Logfire browser configure failed", error);
    });
}
