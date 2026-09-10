import { board, history } from "../../../../delivery-fixtures";
let version = 7;
let latest = 7;
let status = "running";
let failure = "";
export async function GET(request: Request) {
  const path = new URL(request.url).pathname;
  if (failure && path.includes(failure))
    return Response.json(
      { detail: "Controlled refresh outage" },
      { status: 503 }
    );
  return Response.json(
    path.endsWith("qa-history")
      ? history(version, latest, status)
      : board(version)
  );
}
// Local-only controls also support the inline browser acceptance demonstration.
export async function POST(request: Request) {
  const state = await request.json();
  version = state.version ?? version;
  latest = state.latest ?? Math.max(latest, version);
  status = state.status ?? status;
  failure = state.failure ?? "";
  return Response.json({ version, latest, status, failure });
}
