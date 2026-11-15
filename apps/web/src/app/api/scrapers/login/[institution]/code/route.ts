import type { NextRequest } from "next/server";
import { z } from "zod";

import { parseJsonBody } from "@/lib/api/validation";
import { SIGN_IN_CODE_PATTERN } from "@/lib/scraper-login";
import {
  INSTITUTION_SLUG,
  badInstitution,
  forwardLogin,
} from "@/lib/scraper-login-forward";

type Context = { params: Promise<{ institution: string }> };

const codeBody = z.object({
  code: z
    .string()
    .trim()
    .regex(SIGN_IN_CODE_PATTERN, "El código debe tener 6 dígitos"),
});

/**
 * POST /api/scrapers/login/{institution}/code: hand a `code` sign-in the
 * one-time code the institution e-mailed (`{ "code": "000000" }`). `202`
 * once accepted (the sign-in goes on; poll the status route), `409` when the
 * sign-in is not waiting for a code.
 */
export async function POST(request: NextRequest, { params }: Context) {
  const { institution } = await params;
  if (!INSTITUTION_SLUG.test(institution)) return badInstitution();
  const parsed = await parseJsonBody(request, codeBody);
  if (!parsed.ok) return parsed.response;
  return forwardLogin(`/login/${institution}/code`, {
    method: "POST",
    body: { code: parsed.data.code },
  });
}
