import { describe, expect, it } from "vitest";
import type { TransactionBilling } from "@/lib/card-billing";
import {
  INITIAL_LIST_CONTROLS,
  listControlsReducer,
  PAGE_SIZE,
  productTransactionsView,
} from "@/lib/product-transactions-view";

// Synthetic fixtures only: invented descriptions and statuses.

const BILLED: TransactionBilling = {
  status: "billed",
  estimated: false,
  statementDate: "2026-10-15",
};
const BILLED_ESTIMATED: TransactionBilling = {
  status: "billed",
  estimated: true,
  statementDate: null,
};
const UNBILLED: TransactionBilling = {
  status: "unbilled",
  estimated: false,
  statementDate: null,
};

function row(id: string, description: string, billing: TransactionBilling | null) {
  return { id, description, rawDescription: description.toUpperCase(), billing };
}

/** A credit card's movements, newest first. */
const CARD_ROWS = [
  row("a", "Supermercado ficticio", UNBILLED),
  row("b", "Cafetería ficticia", UNBILLED),
  row("c", "Supermercado ficticio", BILLED_ESTIMATED),
  row("d", "Farmacia ficticia", BILLED),
  row("e", "Compra manual", null),
];

type Row = ReturnType<typeof row>;

function view(
  overrides: Partial<Parameters<typeof productTransactionsView<Row>>[0]> = {}
) {
  return productTransactionsView({
    rows: CARD_ROWS,
    search: "",
    billingFilter: "all",
    visible: PAGE_SIZE,
    showBilling: true,
    truncated: false,
    ...overrides,
  });
}

const ids = (rows: readonly { id: string }[]) => rows.map((r) => r.id);

describe("listControlsReducer", () => {
  const paged = { ...INITIAL_LIST_CONTROLS, visible: PAGE_SIZE * 3 };

  it("starts a new search from the first page, keeping the filter", () => {
    expect(
      listControlsReducer(
        { ...paged, billingFilter: "billed" },
        { type: "search", search: "super" }
      )
    ).toEqual({ search: "super", billingFilter: "billed", visible: PAGE_SIZE });
  });

  it("starts a new billing filter from the first page, keeping the search", () => {
    expect(
      listControlsReducer(
        { ...paged, search: "super" },
        { type: "billingFilter", billingFilter: "unbilled" }
      )
    ).toEqual({ search: "super", billingFilter: "unbilled", visible: PAGE_SIZE });
  });

  it("renders one more page on Ver más", () => {
    expect(
      listControlsReducer(INITIAL_LIST_CONTROLS, { type: "showMore" }).visible
    ).toBe(PAGE_SIZE * 2);
  });
});

describe("productTransactionsView", () => {
  it("keeps the rows that match both the search and the billing filter", () => {
    expect(ids(view({ search: "super" }).matching)).toEqual(["a", "c"]);
    expect(ids(view({ billingFilter: "unbilled" }).matching)).toEqual(["a", "b"]);
    expect(
      ids(view({ search: "super", billingFilter: "unbilled" }).matching)
    ).toEqual(["a"]);
    // The search reads the scraped text too.
    expect(ids(view({ search: "FARMACIA" }).matching)).toEqual(["d"]);
  });

  it("counts the matches in the description while filtering", () => {
    expect(view().description).toBe("5 transacciones");
    expect(view({ billingFilter: "billed" }).description).toBe(
      "2 de 5 transacciones"
    );
    expect(view({ search: "super", billingFilter: "unbilled" }).description).toBe(
      "1 de 5 transacciones"
    );
    expect(view({ rows: CARD_ROWS.slice(0, 1) }).description).toBe(
      "1 transacción"
    );
  });

  it("says the counts cover only the newest movements of a cut list", () => {
    const cut = view({ truncated: true });
    expect(cut.description).toBe("Las 5 transacciones más recientes");
    expect(cut.coverageNote).toBeNull();

    const filtered = view({ truncated: true, billingFilter: "billed" });
    expect(filtered.description).toBe("2 de las 5 transacciones más recientes");
    expect(filtered.coverageNote).toBe(
      "El filtro cubre las 5 transacciones más recientes."
    );
    expect(view({ truncated: true, search: "super" }).coverageNote).toBe(
      "La búsqueda cubre las 5 transacciones más recientes."
    );
    expect(view({ search: "super" }).coverageNote).toBeNull();
  });

  it("ignores the billing filter where the list shows no status", () => {
    const checking = view({ showBilling: false, billingFilter: "billed" });
    expect(checking.showsBilling).toBe(false);
    expect(checking.billingFilter).toBe("all");
    expect(checking.matching).toHaveLength(5);
    expect(checking.description).toBe("5 transacciones");

    const unknown = view({
      rows: [row("x", "Compra manual", null)],
      billingFilter: "unbilled",
    });
    expect(unknown.showsBilling).toBe(false);
    expect(unknown.matching).toHaveLength(1);
  });

  it("says why the table is empty", () => {
    expect(view({ rows: [] }).emptyMessage).toBe(
      "Sin transacciones registradas para este producto."
    );
    expect(view({ rows: [] }).description).toBe(
      "Movimientos registrados para este producto"
    );
    expect(view({ search: "  hotel " }).emptyMessage).toBe(
      "Sin resultados para «hotel»."
    );
    const billedOnly = CARD_ROWS.filter((r) => r.billing?.status === "billed");
    expect(
      view({ rows: billedOnly, billingFilter: "unbilled" }).emptyMessage
    ).toBe("Sin movimientos no facturados.");
    const unbilledOnly = CARD_ROWS.filter((r) => r.billing?.status === "unbilled");
    expect(
      view({ rows: unbilledOnly, billingFilter: "billed" }).emptyMessage
    ).toBe("Sin movimientos facturados.");
    expect(view().emptyMessage).toBeNull();
  });

  it("pages the matches", () => {
    const first = view({ visible: 2 });
    expect(ids(first.shown)).toEqual(["a", "b"]);
    expect(first.pagingNote).toBe("Mostrando 2 de 5");
    expect(view({ visible: 1, billingFilter: "billed" }).pagingNote).toBe(
      "Mostrando 1 de 2 resultados"
    );
    expect(view().pagingNote).toBeNull();
  });

  it("explains the estimate border only while an estimate is shown", () => {
    expect(view().showsEstimateLegend).toBe(true);
    expect(view({ visible: 2 }).showsEstimateLegend).toBe(false);
    expect(view({ billingFilter: "unbilled" }).showsEstimateLegend).toBe(false);
  });
});
