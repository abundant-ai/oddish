import { proxyBackendJson } from "@/lib/backend-response";

type SummaryRouteContext = {
  params: Promise<{ trial_id: string }>;
};

async function forward(
  request: Request,
  { params }: SummaryRouteContext,
  method: "GET" | "POST"
) {
  const { trial_id } = await params;
  return proxyBackendJson({
    request,
    path: `trials/${encodeURIComponent(trial_id)}/trajectory/summary`,
    method,
    signal: request.signal,
  });
}

export function GET(request: Request, context: SummaryRouteContext) {
  return forward(request, context, "GET");
}

export function POST(request: Request, context: SummaryRouteContext) {
  return forward(request, context, "POST");
}
