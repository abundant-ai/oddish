import { NextResponse } from "next/server";
import { promises as fs } from "node:fs";
import path from "node:path";

// process.cwd() is the frontend dir when running `pnpm dev`, so go up one
// level to land at the repo root. Allow an env override for non-standard
// invocations (e.g. running from a different working dir).
const PRESETS_PATH =
  process.env.ODDISH_PROBE_PRESETS_PATH ??
  path.join(process.cwd(), "..", "probe-presets.json");

export async function GET() {
  try {
    const raw = await fs.readFile(PRESETS_PATH, "utf-8");
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      return NextResponse.json(
        { error: "presets file is not an array" },
        { status: 500 },
      );
    }
    return NextResponse.json(parsed);
  } catch (err: unknown) {
    if (
      err &&
      typeof err === "object" &&
      "code" in err &&
      (err as { code?: string }).code === "ENOENT"
    ) {
      // File doesn't exist yet — return empty array
      return NextResponse.json([]);
    }
    return NextResponse.json({ error: String(err) }, { status: 500 });
  }
}

export async function PUT(req: Request) {
  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "body must be JSON" }, { status: 400 });
  }
  if (!Array.isArray(body)) {
    return NextResponse.json(
      { error: "body must be an array of presets" },
      { status: 400 },
    );
  }
  try {
    await fs.writeFile(
      PRESETS_PATH,
      JSON.stringify(body, null, 2) + "\n",
      "utf-8",
    );
    return NextResponse.json({ ok: true, count: body.length });
  } catch (err) {
    return NextResponse.json({ error: String(err) }, { status: 500 });
  }
}
