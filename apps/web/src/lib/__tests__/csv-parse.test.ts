import { describe, it, expect } from "vitest";
import {
  DEFAULT_MAPPING,
  detectColumns,
  parseAmount,
  parseDate,
  splitCsvRows,
  toParsedRows,
  type ColumnMapping,
} from "@/lib/csv-parse";

// Every figure and description below is synthetic.

describe("splitCsvRows", () => {
  it.each([
    [
      "semicolon",
      "Fecha;Glosa\n05/03/2026;Supermercado de prueba",
      [["Fecha", "Glosa"], ["05/03/2026", "Supermercado de prueba"]],
    ],
    [
      "comma",
      "Fecha,Glosa\n05/03/2026,Supermercado de prueba",
      [["Fecha", "Glosa"], ["05/03/2026", "Supermercado de prueba"]],
    ],
    [
      "semicolon when the header has one, keeping decimal commas",
      "Fecha;Monto\n05/03/2026;-1.234,56",
      [["Fecha", "Monto"], ["05/03/2026", "-1.234,56"]],
    ],
  ])("splits on %s", (_name, text, expected) => {
    expect(splitCsvRows(text)).toEqual(expected);
  });

  it("strips surrounding quotes and trims cells", () => {
    expect(splitCsvRows('"Fecha";"Glosa"\n"05/03/2026";" Pago de prueba "')).toEqual([
      ["Fecha", "Glosa"],
      ["05/03/2026", "Pago de prueba"],
    ]);
  });

  it("drops blank lines and CRLF endings", () => {
    expect(splitCsvRows("Fecha;Monto\r\n\r\n   \n05/03/2026;999.999\r\n")).toEqual([
      ["Fecha", "Monto"],
      ["05/03/2026", "999.999"],
    ]);
  });

  it("returns no rows for empty text", () => {
    expect(splitCsvRows(" \n \n")).toEqual([]);
  });
});

describe("detectColumns", () => {
  const dateDescAmount = { description: 1, amount: 2, date: 0 };

  it.each<[string, string[], ColumnMapping]>([
    ["Spanish headers", ["Fecha", "Glosa", "Monto"], dateDescAmount],
    ["any case", ["DATE", "Description", "AMOUNT"], dateDescAmount],
    [
      "the leftmost matching header",
      ["Dia", "Detalle", "Abono", "Cargo"],
      dateDescAmount,
    ],
    [
      "fallbacks for missing fields only",
      ["Nota", "Otro", "Fecha", "Valor"],
      { description: 0, amount: 3, date: 2 },
    ],
    ["the default mapping when nothing matches", ["a", "b", "c"], DEFAULT_MAPPING],
  ])("picks %s", (_name, headers, expected) => {
    expect(detectColumns(headers)).toEqual(expected);
  });
});

describe("parseAmount", () => {
  it.each([
    ["1.234", 1234],
    ["-1.234,56", -1235],
    ["$ 2.500.000", 2_500_000],
    ["999.999", 999_999],
    ["", 0],
    ["abc", 0],
  ])("parses %j as %d", (raw, expected) => {
    expect(parseAmount(raw)).toBe(expected);
  });

  it("does not support US-style separators (current behavior)", () => {
    expect(parseAmount("1,234.56")).toBe(1);
  });
});

describe("parseDate", () => {
  it.each([
    ["05/03/2026", "2026-03-05"],
    ["5-3-2026", "2026-03-05"],
    ["2026-03-05", "2026-03-05"],
    ["2026/03/05", "2026-03-05"],
    ["2026-03-05 14:30:00", "2026-03-05"],
    ["2026/03/05 14:30:00", "2026-03-05"],
    ["ayer", "ayer"],
  ])("reads %j as %j", (raw, expected) => {
    expect(parseDate(raw)).toBe(expected);
  });
});

describe("toParsedRows", () => {
  const mapping: ColumnMapping = { description: 1, amount: 2, date: 0 };

  it("reads each row through the mapping", () => {
    expect(
      toParsedRows([["05/03/2026", "Supermercado de prueba", "-999.999"]], mapping)
    ).toEqual([
      { description: "Supermercado de prueba", amount: -999_999, date: "2026-03-05" },
    ]);
  });

  it.each([
    ["an empty description", ["05/03/2026", "", "1.000.000"]],
    ["a zero amount", ["05/03/2026", "Pago de prueba", "0"]],
    ["an empty date", ["", "Pago de prueba", "1.000.000"]],
    ["a missing amount cell", ["05/03/2026", "Pago de prueba"]],
  ])("drops a row with %s", (_name, row) => {
    expect(toParsedRows([row], mapping)).toEqual([]);
  });
});
