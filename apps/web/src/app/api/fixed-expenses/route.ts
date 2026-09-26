import { NextRequest, NextResponse } from "next/server";
import { db } from "@/lib/db";
import { fixedExpenses } from "@/lib/db/schema";
import { asc, eq } from "drizzle-orm";
import {
  mergeFixedExpenseUpdate,
  validateFixedExpenseId,
  validateFixedExpenseInput,
} from "@/lib/fixed-expenses";

type ReadJson = { ok: true; body: unknown } | { ok: false; response: NextResponse };

/** The parsed body, or the 400 to answer when it is not valid JSON. */
async function readJson(request: NextRequest): Promise<ReadJson> {
  try {
    return { ok: true, body: await request.json() };
  } catch {
    return {
      ok: false,
      response: NextResponse.json({ error: "Invalid JSON body" }, { status: 400 }),
    };
  }
}

function badRequest(failure: { error: string; field?: string }) {
  return NextResponse.json(
    { error: failure.error, field: failure.field },
    { status: 400 }
  );
}

/** GET /api/fixed-expenses: every fixed expense by name, including ended and
 *  scheduled ones; the page decides which are active on the local day. */
export async function GET() {
  const rows = await db
    .select()
    .from(fixedExpenses)
    .orderBy(asc(fixedExpenses.name));

  return NextResponse.json(rows);
}

/** POST /api/fixed-expenses: create a fixed expense */
export async function POST(request: NextRequest) {
  const json = await readJson(request);
  if (!json.ok) return json.response;

  const result = validateFixedExpenseInput(json.body, "create");
  if (!result.ok) return badRequest(result);

  const [created] = await db
    .insert(fixedExpenses)
    .values(result.value)
    .returning();

  return NextResponse.json(created, { status: 201 });
}

/** PUT /api/fixed-expenses: update a fixed expense */
export async function PUT(request: NextRequest) {
  const json = await readJson(request);
  if (!json.ok) return json.response;

  const result = validateFixedExpenseInput(json.body, "update");
  if (!result.ok) return badRequest(result);
  const { id, fields } = result.value;

  const [existing] = await db
    .select()
    .from(fixedExpenses)
    .where(eq(fixedExpenses.id, id));
  if (!existing) {
    return NextResponse.json({ error: "Not found" }, { status: 404 });
  }

  const merged = mergeFixedExpenseUpdate(existing, fields);
  if (!merged.ok) return badRequest(merged);

  const [updated] = await db
    .update(fixedExpenses)
    .set({ ...merged.value, updatedAt: new Date() })
    .where(eq(fixedExpenses.id, id))
    .returning();

  if (!updated) {
    return NextResponse.json({ error: "Not found" }, { status: 404 });
  }

  return NextResponse.json(updated);
}

/** DELETE /api/fixed-expenses: delete a fixed expense */
export async function DELETE(request: NextRequest) {
  const json = await readJson(request);
  if (!json.ok) return json.response;

  const result = validateFixedExpenseId(json.body);
  if (!result.ok) return badRequest(result);

  await db.delete(fixedExpenses).where(eq(fixedExpenses.id, result.value.id));

  return NextResponse.json({ ok: true });
}
