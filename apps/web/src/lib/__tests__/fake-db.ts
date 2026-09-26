// A recording stand-in for the Drizzle client, so route tests run without
// Postgres. Each entry point (select/insert/update/delete) starts a chain that
// records every builder call and, when awaited, resolves to what the current
// responder returns for it. By default inserts and updates echo their values
// back as the returned row, and everything else resolves to no rows.

import { PgDialect } from "drizzle-orm/pg-core";
import type { SQL } from "drizzle-orm";

/** Synthetic id the default responder gives every returned row. */
export const FAKE_ROW_ID = "00000000-0000-4000-8000-000000000001";

type Op = "select" | "insert" | "update" | "delete";

export interface RecordedQuery {
  op: Op;
  calls: { method: string; args: unknown[] }[];
}

export type Responder = (query: RecordedQuery) => unknown;

/** First argument of the first `method` call in the chain (e.g. `values`). */
export function argOf(query: RecordedQuery, method: string): unknown {
  return query.calls.find((call) => call.method === method)?.args[0];
}

/** Bound parameters of the chain's `where` condition, in SQL order. */
export function whereParams(query: RecordedQuery): unknown[] {
  return new PgDialect().sqlToQuery(argOf(query, "where") as SQL).params;
}

const echoRows: Responder = (query) => {
  if (query.op === "insert") {
    return [{ id: FAKE_ROW_ID, ...(argOf(query, "values") as object) }];
  }
  if (query.op === "update") {
    return [{ id: FAKE_ROW_ID, ...(argOf(query, "set") as object) }];
  }
  return [];
};

export function createFakeDb() {
  const queries: RecordedQuery[] = [];
  let respond: Responder = echoRows;

  const start =
    (op: Op) =>
    (...args: unknown[]) => {
      const query: RecordedQuery = { op, calls: [{ method: op, args }] };
      queries.push(query);
      const chain: object = new Proxy(
        {},
        {
          get(_target, prop) {
            if (prop === "then") {
              return (
                onFulfilled?: (value: unknown) => unknown,
                onRejected?: (reason: unknown) => unknown
              ) =>
                Promise.resolve()
                  .then(() => respond(query))
                  .then(onFulfilled, onRejected);
            }
            return (...callArgs: unknown[]) => {
              query.calls.push({ method: String(prop), args: callArgs });
              return chain;
            };
          },
        }
      );
      return chain;
    };

  return {
    db: {
      select: start("select"),
      insert: start("insert"),
      update: start("update"),
      delete: start("delete"),
    },
    queries,
    respondWith(responder: Responder) {
      respond = responder;
    },
    reset() {
      queries.length = 0;
      respond = echoRows;
    },
  };
}
