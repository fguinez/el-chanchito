// Request validation for the API routes: read a JSON body or the query
// string, check it against a zod schema, and turn any failure into a 400 the
// route returns as is. Failures share the monitors API's `{ error, field }`
// shape and add every issue under `issues`.

import { NextResponse, type NextRequest } from "next/server";
import { z } from "zod";

/** One schema failure; `path` uses the `rows[0].amount` form ("" for the root). */
export interface ApiIssue {
  path: string;
  message: string;
}

/** Validated data, or the 400 response to return instead. */
export type ParseResult<T> =
  | { ok: true; data: T }
  | { ok: false; response: NextResponse };

/** `["rows", 0, "amount"]` -> `rows[0].amount`. */
export function formatPath(path: readonly PropertyKey[]): string {
  return path.reduce<string>((out, key) => {
    if (typeof key === "number") return `${out}[${key}]`;
    return out ? `${out}.${String(key)}` : String(key);
  }, "");
}

/** 400 `{ error, field?, issues }`; `error` names the first failing field so
 *  it reads on its own in a toast or a curl session. */
export function validationErrorResponse(error: z.ZodError): NextResponse {
  const issues: ApiIssue[] = error.issues.map((issue) => ({
    path: formatPath(issue.path),
    message: issue.message,
  }));
  const first = issues[0] ?? { path: "", message: "Invalid request" };
  return NextResponse.json(
    {
      error: first.path ? `${first.path}: ${first.message}` : first.message,
      field: first.path || undefined,
      issues,
    },
    { status: 400 }
  );
}

function check<S extends z.ZodType>(
  schema: S,
  input: unknown
): ParseResult<z.output<S>> {
  const parsed = schema.safeParse(input);
  if (!parsed.success) {
    return { ok: false, response: validationErrorResponse(parsed.error) };
  }
  return { ok: true, data: parsed.data };
}

/** Read the JSON body and check it; an empty or malformed body is a 400 too. */
export async function parseJsonBody<S extends z.ZodType>(
  request: Request,
  schema: S
): Promise<ParseResult<z.output<S>>> {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return {
      ok: false,
      response: NextResponse.json({ error: "Invalid JSON body" }, { status: 400 }),
    };
  }
  return check(schema, body);
}

/** Check the query string; a repeated key keeps its last value. */
export function parseSearchParams<S extends z.ZodType>(
  params: URLSearchParams,
  schema: S
): ParseResult<z.output<S>> {
  return check(schema, Object.fromEntries(params));
}

/** A route handler that only ever runs on a body that passed `schema`. */
export function withJsonBody<S extends z.ZodType>(
  schema: S,
  handler: (body: z.output<S>, request: NextRequest) => Promise<Response>
): (request: NextRequest) => Promise<Response> {
  return async (request) => {
    const parsed = await parseJsonBody(request, schema);
    if (!parsed.ok) return parsed.response;
    return handler(parsed.data, request);
  };
}
