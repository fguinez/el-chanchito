import type { NextRequest } from "next/server";

import { requireJsonContentType } from "@/lib/api/validation";
import {
  INSTITUTION_SLUG,
  badInstitution,
  forwardLogin,
} from "@/lib/scraper-login-forward";

type Context = { params: Promise<{ institution: string }> };

/**
 * GET /api/scrapers/login/{institution}: the latest dashboard-started sign-in
 * of that institution (`state`: idle, running, awaiting_code, succeeded or
 * failed, with a `message` when it failed). The UI polls it while a sign-in
 * is under way.
 */
export async function GET(_request: Request, { params }: Context) {
  const { institution } = await params;
  if (!INSTITUTION_SLUG.test(institution)) return badInstitution();
  return forwardLogin(`/login/${institution}`, { method: "GET" });
}

/**
 * POST /api/scrapers/login/{institution}: start the interactive sign-in (the
 * `make *-login` flow) on the scraper service. `202` once started; `409` when
 * one is already under way or a run of that institution holds it; `404` when
 * the institution has no interactive sign-in. The body must be labelled JSON
 * (`{}`), so a cross-origin form post cannot open a sign-in window or mail a
 * code (the proxy's Origin check is the other half).
 */
export async function POST(request: NextRequest, { params }: Context) {
  const unsupported = requireJsonContentType(request);
  if (unsupported) return unsupported;
  const { institution } = await params;
  if (!INSTITUTION_SLUG.test(institution)) return badInstitution();
  return forwardLogin(`/login/${institution}`, { method: "POST", body: {} });
}
