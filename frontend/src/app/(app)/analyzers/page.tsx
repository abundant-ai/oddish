import { AnalyzerCostsPanel, ReportsClient } from "./reports-client";

export const metadata = { title: "Analyzers" };

export default function AnalyzersPage() {
  return (
    <div className="space-y-4">
      <AnalyzerCostsPanel />
      <ReportsClient />
    </div>
  );
}
