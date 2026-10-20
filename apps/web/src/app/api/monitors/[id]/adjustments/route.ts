import { NextRequest, NextResponse } from "next/server";
import { eq } from "drizzle-orm";
import { db } from "@/lib/db";
import { monitorAdjustments, monitors, transactions } from "@/lib/db/schema";
import { getRateBook } from "@/lib/rate-book";
import {
  loadMonitorAdjustments,
  loadProductCatalog,
} from "@/lib/monitors/catalog";
import { UUID_RE } from "@/lib/monitors/validate";
import {
  isForeignKeyViolation,
  isUniqueViolation,
  parseMonthParam,
} from "@/lib/monitors/adjustments";
import { monitorProductIds, withEffects } from "@/lib/monitors/effects";
import { createMonitorAdjustmentSchema } from "@/lib/api/schemas";
import { failureBody, parseJsonBody } from "@/lib/api/validation";

// Next 16: dynamic segment params arrive as a Promise on the context arg.
type Context = { params: Promise<{ id: string }> };

/**
 * GET /api/monitors/[id]/adjustments: the monitor's variaciones (linked
 * transactions), oldest transaction day first, each with its transaction's
 * display description, date, amount and product, a `deleted` flag for a
 * soft-deleted transaction, and its current `effect` and `monthToDateEffect`
 * on the alert threshold (monitor currency). `?month=YYYY-MM` keeps one month.
 */
export async function GET(request: NextRequest, { params }: Context) {
  try {
    const { id } = await params;
    if (!UUID_RE.test(id)) {
      return NextResponse.json({ error: "Invalid id" }, { status: 400 });
    }
    const month = parseMonthParam(request.nextUrl.searchParams.get("month"));
    if (!month.ok) {
      return NextResponse.json(
        failureBody({ error: month.error, field: month.field }),
        { status: month.status }
      );
    }
    const [monitor] = await db
      .select({
        expression: monitors.expression,
        thresholds: monitors.thresholds,
        currency: monitors.currency,
      })
      .from(monitors)
      .where(eq(monitors.id, id));
    if (!monitor) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }

    const [catalog, rates, rows] = await Promise.all([
      loadProductCatalog(),
      getRateBook(),
      loadMonitorAdjustments(id),
    ]);
    const adjustments = withEffects(rows, monitor, {
      date: new Date(),
      products: catalog.byId,
      rates,
      currency: monitor.currency,
    });
    const selected = month.value;
    return NextResponse.json({
      adjustments:
        selected == null
          ? adjustments
          : adjustments.filter((a) =>
              a.transactionDate.startsWith(`${selected}-`)
            ),
    });
  } catch (error) {
    console.error("GET /api/monitors/[id]/adjustments failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}

/**
 * POST /api/monitors/[id]/adjustments: ignore a transaction for this monitor,
 * from `{ transactionId }`. 400 `{ error, field, issues }` when the
 * transaction does not exist, was deleted, or belongs to a product neither
 * the left expression nor any threshold references; 409 when it is already
 * linked; 404 if the monitor is gone. 201 with the stored link.
 */
export async function POST(request: NextRequest, { params }: Context) {
  try {
    const { id } = await params;
    if (!UUID_RE.test(id)) {
      return NextResponse.json({ error: "Invalid id" }, { status: 400 });
    }
    const parsed = await parseJsonBody(request, createMonitorAdjustmentSchema);
    if (!parsed.ok) return parsed.response;
    const { transactionId } = parsed.data;

    const [monitor] = await db
      .select({ expression: monitors.expression, thresholds: monitors.thresholds })
      .from(monitors)
      .where(eq(monitors.id, id));
    if (!monitor) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }

    const [txn] = await db
      .select({
        productId: transactions.productId,
        deletedAt: transactions.deletedAt,
      })
      .from(transactions)
      .where(eq(transactions.id, transactionId));
    if (!txn || txn.deletedAt != null) {
      return NextResponse.json(
        failureBody({
          error: "Transaction not found",
          field: "transactionId",
        }),
        { status: 400 }
      );
    }
    if (!monitorProductIds(monitor).includes(txn.productId)) {
      return NextResponse.json(
        failureBody({
          error: "The transaction's product is not referenced by the monitor",
          field: "transactionId",
        }),
        { status: 400 }
      );
    }

    const [created] = await db
      .insert(monitorAdjustments)
      .values({ monitorId: id, transactionId })
      .returning();
    return NextResponse.json(created, { status: 201 });
  } catch (error) {
    if (isUniqueViolation(error)) {
      return NextResponse.json(
        failureBody({
          error: "The transaction is already a variation of this monitor",
          field: "transactionId",
        }),
        { status: 409 }
      );
    }
    if (isForeignKeyViolation(error)) {
      // Deleted between the checks and the insert: the monitor (404) or the
      // transaction (a bad transactionId).
      const { id } = await params;
      const [monitor] = await db
        .select({ id: monitors.id })
        .from(monitors)
        .where(eq(monitors.id, id))
        .catch(() => []);
      if (!monitor) {
        return NextResponse.json({ error: "Not found" }, { status: 404 });
      }
      return NextResponse.json(
        failureBody({ error: "Transaction not found", field: "transactionId" }),
        { status: 400 }
      );
    }
    console.error("POST /api/monitors/[id]/adjustments failed:", error);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
