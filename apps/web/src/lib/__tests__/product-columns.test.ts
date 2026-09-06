import { describe, it, expect } from "vitest";
import {
  FAMILY_INFO,
  type ColumnSpec,
  type ProductFamily,
  type ProductKind,
} from "@chanchito/product-model";
import {
  formatAmount,
  formatCLP,
  formatPercent,
  formatPlainDateEs,
} from "../utils";
import {
  groupProductsByFamily,
  resolveColumnCell,
  showKindColumn,
  visibleColumns,
  type ColumnProduct,
} from "../product-columns";

// Synthetic fixtures only: obviously fake CLP amounts (2.500.000 / 1.000.000 /
// 999.999), USD 1.234,56 and a made-up BTC quantity.
function product(
  kind: ProductKind,
  overrides: Partial<ColumnProduct> = {}
): ColumnProduct {
  return {
    kind,
    currency: "CLP",
    currentBalance: 2_500_000,
    currentBalanceClp: 2_500_000,
    attributes: { kind },
    metrics: null,
    ...overrides,
  };
}

/** The real registry column, so a renamed key fails here and not in the UI. */
function column(family: ProductFamily, key: string): ColumnSpec {
  const spec = FAMILY_INFO[family].columns.find((c) => c.key === key);
  if (!spec) throw new Error(`no column ${key} in family ${family}`);
  return spec;
}

describe("groupProductsByFamily", () => {
  it("buckets by family in registry order, keeping incoming row order", () => {
    const groups = groupProductsByFamily([
      product("investment"),
      product("checking"),
      product("credit_card"),
      product("savings"),
    ]);
    expect(groups.map((g) => g.family)).toEqual([
      "cash",
      "revolving_credit",
      "investment",
    ]);
    expect(groups[0].products.map((p) => p.kind)).toEqual([
      "checking",
      "savings",
    ]);
  });

  it("returns no groups for no products", () => {
    expect(groupProductsByFamily([])).toEqual([]);
  });
});

describe("showKindColumn", () => {
  it("is true only when rows span more than one kind", () => {
    expect(showKindColumn([product("checking"), product("savings")])).toBe(true);
    expect(showKindColumn([product("checking"), product("checking")])).toBe(
      false
    );
    expect(showKindColumn([])).toBe(false);
  });
});

describe("visibleColumns", () => {
  it("hides an optional column no row fills", () => {
    const rows = [
      product("checking", { metrics: { kind: "checking", balance: 2_500_000 } }),
      product("savings", { metrics: { kind: "savings", balance: 1_000_000 } }),
    ];
    expect(visibleColumns("cash", rows).map((c) => c.key)).toEqual(["saldo"]);
  });

  it("shows an optional column as soon as one row fills it", () => {
    const rows = [
      product("checking", { metrics: { kind: "checking", balance: 2_500_000 } }),
      product("vista", {
        metrics: { kind: "vista", balance: 1_000_000, accounting_balance: 999_999 },
      }),
    ];
    expect(visibleColumns("cash", rows).map((c) => c.key)).toEqual([
      "saldo",
      "saldo_contable",
    ]);
  });

  it("keeps required columns even when every row is empty", () => {
    const rows = [
      product("credit_card", {
        metrics: { kind: "credit_card", available: 1_000_000 },
      }),
    ];
    expect(visibleColumns("revolving_credit", rows).map((c) => c.key)).toEqual([
      "disponible",
      "cupo",
      "utilizado",
    ]);
  });

  it("can hide every column of a family whose rows carry no values", () => {
    const rows = [
      product("debit_card", { currentBalance: null, currentBalanceClp: null }),
    ];
    expect(visibleColumns("other", rows)).toEqual([]);
  });
});

describe("resolveColumnCell", () => {
  describe("headline", () => {
    it("formats in the product currency with a CLP sub-line for non-CLP", () => {
      const usd = product("investment", {
        currency: "USD",
        currentBalance: 1234.56,
        currentBalanceClp: 1_000_000,
      });
      const cell = resolveColumnCell(usd, column("investment", "valor"));
      expect(cell.text).toBe(formatAmount("USD", 1234.56));
      expect(cell.clp).toBe(1_000_000);
      expect(cell.sortValue).toBe(1_000_000);
      expect(cell.tone).toBeNull();
    });

    it("has no CLP sub-line for CLP products", () => {
      const cell = resolveColumnCell(product("checking"), column("cash", "saldo"));
      expect(cell.text).toBe(formatCLP(2_500_000));
      expect(cell.clp).toBeNull();
      expect(cell.sortValue).toBe(2_500_000);
    });

    it("is empty without a balance", () => {
      const cell = resolveColumnCell(
        product("checking", { currentBalance: null, currentBalanceClp: null }),
        column("cash", "saldo")
      );
      expect(cell).toEqual({ sortValue: null, text: null, clp: null, tone: null });
    });
  });

  describe("clp_value", () => {
    it("formats the CLP-converted balance", () => {
      const btc = product("crypto", {
        currency: "BTC",
        currentBalance: 0.0421,
        currentBalanceClp: 999_999,
      });
      const cell = resolveColumnCell(btc, column("crypto", "clp"));
      expect(cell.text).toBe(formatCLP(999_999));
      expect(cell.sortValue).toBe(999_999);
      expect(cell.clp).toBeNull();
    });

    it("is empty when there is no conversion", () => {
      const btc = product("crypto", {
        currency: "BTC",
        currentBalance: 0.0421,
        currentBalanceClp: null,
      });
      expect(resolveColumnCell(btc, column("crypto", "clp")).text).toBeNull();
    });
  });

  describe("metric", () => {
    it("formats currency metrics in the product currency", () => {
      const card = product("credit_card", {
        currentBalance: 1_000_000,
        metrics: { kind: "credit_card", available: 1_000_000, limit: 2_500_000, owed: 999_999 },
      });
      expect(resolveColumnCell(card, column("revolving_credit", "cupo")).text).toBe(
        formatCLP(2_500_000)
      );
      const utilizado = resolveColumnCell(card, column("revolving_credit", "utilizado"));
      expect(utilizado.text).toBe(formatCLP(999_999));
      expect(utilizado.sortValue).toBe(999_999);

      const usd = product("investment", {
        currency: "USD",
        metrics: { kind: "investment", nav: 1234.56, deposited: 1000.5 },
      });
      expect(resolveColumnCell(usd, column("investment", "aportado")).text).toBe(
        formatAmount("USD", 1000.5)
      );
    });

    it("renders signed percents with an explicit sign and a tone", () => {
      const up = product("investment", {
        metrics: { kind: "investment", nav: 2_500_000, var_30d_pct: 2.1 },
      });
      const upCell = resolveColumnCell(up, column("investment", "var_30d"));
      expect(upCell.text).toBe(formatPercent(2.1, { signed: true }));
      expect(upCell.text).toMatch(/^\+2[.,]1%$/);
      expect(upCell.tone).toBe("positive");
      expect(upCell.sortValue).toBe(2.1);

      const down = product("investment", {
        metrics: { kind: "investment", nav: 2_500_000, var_30d_pct: -0.35 },
      });
      const downCell = resolveColumnCell(down, column("investment", "var_30d"));
      expect(downCell.text).toMatch(/^-0[.,]35%$/);
      expect(downCell.tone).toBe("negative");

      const flat = product("investment", {
        metrics: { kind: "investment", nav: 2_500_000, var_30d_pct: 0 },
      });
      const flatCell = resolveColumnCell(flat, column("investment", "var_30d"));
      expect(flatCell.text).toMatch(/^0[.,]0%$/);
      expect(flatCell.tone).toBeNull();
    });

    it("prefixes positive signed currency values with +", () => {
      const gain = product("investment", {
        metrics: { kind: "investment", nav: 2_500_000, profit: 999_999 },
      });
      const gainCell = resolveColumnCell(gain, column("investment", "ganancia"));
      expect(gainCell.text).toBe(`+${formatCLP(999_999)}`);
      expect(gainCell.tone).toBe("positive");

      const loss = product("investment", {
        metrics: { kind: "investment", nav: 2_500_000, profit: -999_999 },
      });
      const lossCell = resolveColumnCell(loss, column("investment", "ganancia"));
      expect(lossCell.text).toBe(formatCLP(-999_999));
      expect(lossCell.tone).toBe("negative");
    });

    it("leaves unsigned columns untoned", () => {
      const cell = resolveColumnCell(
        product("investment", {
          metrics: { kind: "investment", nav: 2_500_000, deposited: 1_000_000 },
        }),
        column("investment", "aportado")
      );
      expect(cell.tone).toBeNull();
      expect(cell.text).toBe(formatCLP(1_000_000));
    });

    it("is empty for legacy {} / null metrics and non-numeric values", () => {
      const cupo = column("revolving_credit", "cupo");
      expect(resolveColumnCell(product("credit_card", { metrics: {} }), cupo).text).toBeNull();
      expect(resolveColumnCell(product("credit_card", { metrics: null }), cupo).text).toBeNull();
      expect(
        resolveColumnCell(
          product("credit_card", {
            metrics: { kind: "credit_card", available: 1_000_000, limit: "2500000" },
          }),
          cupo
        ).text
      ).toBeNull();
    });
  });

  describe("attribute", () => {
    it("formats date attributes as es-CL dates and sorts by the ISO string", () => {
      const deposit = product("term_deposit", {
        attributes: { kind: "term_deposit", maturity_date: "2026-12-31" },
      });
      const cell = resolveColumnCell(deposit, column("term_deposit", "vencimiento"));
      expect(cell.text).toBe(formatPlainDateEs("2026-12-31"));
      expect(cell.text).toMatch(/^31\D+12\D+2026$/);
      expect(cell.sortValue).toBe("2026-12-31");
    });

    it("formats percent attributes without a forced sign", () => {
      const deposit = product("term_deposit", {
        attributes: { kind: "term_deposit", interest_rate_pct: 4.2 },
      });
      const cell = resolveColumnCell(deposit, column("term_deposit", "tasa"));
      expect(cell.text).toBe(formatPercent(4.2));
      expect(cell.text).toMatch(/^4[.,]2%$/);
      expect(cell.tone).toBeNull();
    });

    it("is empty when the attribute is missing", () => {
      const cell = resolveColumnCell(
        product("term_deposit"),
        column("term_deposit", "vencimiento")
      );
      expect(cell.text).toBeNull();
      expect(cell.sortValue).toBeNull();
    });
  });

  describe("installments", () => {
    const cuotas = column("installment_loan", "cuotas");

    it("shows paid / total when both are known", () => {
      const loan = product("loan", {
        attributes: { kind: "loan", installments_total: 24 },
        metrics: { kind: "loan", owed: 2_500_000, installments_paid: 6 },
      });
      const cell = resolveColumnCell(loan, cuotas);
      expect(cell.text).toBe("6 / 24");
      expect(cell.sortValue).toBe(6);
    });

    it("degrades to whichever side is known", () => {
      const onlyPaid = product("loan", {
        metrics: { kind: "loan", owed: 2_500_000, installments_paid: 6 },
      });
      expect(resolveColumnCell(onlyPaid, cuotas).text).toBe("6");

      const onlyTotal = product("loan", {
        attributes: { kind: "loan", installments_total: 24 },
        metrics: { kind: "loan", owed: 2_500_000 },
      });
      const cell = resolveColumnCell(onlyTotal, cuotas);
      expect(cell.text).toBe("— / 24");
      expect(cell.sortValue).toBeNull();
    });

    it("is empty when neither side is known", () => {
      expect(resolveColumnCell(product("loan"), cuotas).text).toBeNull();
      expect(
        resolveColumnCell(product("loan", { metrics: {} }), cuotas).text
      ).toBeNull();
    });
  });
});

describe("formatters", () => {
  it("formatPercent keeps one to two decimals and signs on request", () => {
    expect(formatPercent(4.2)).toMatch(/^4[.,]2%$/);
    expect(formatPercent(4)).toMatch(/^4[.,]0%$/);
    expect(formatPercent(1.234)).toMatch(/^1[.,]23%$/);
    expect(formatPercent(2.1, { signed: true })).toMatch(/^\+2[.,]1%$/);
    expect(formatPercent(-0.35, { signed: true })).toMatch(/^-0[.,]35%$/);
    expect(formatPercent(0, { signed: true })).toMatch(/^0[.,]0%$/);
  });

  it("formatPlainDateEs renders day-month-year as local time", () => {
    expect(formatPlainDateEs("2026-12-31")).toMatch(/^31\D+12\D+2026$/);
    expect(formatPlainDateEs("2026-01-01")).toMatch(/^0?1\D+0?1\D+2026$/);
  });
});
