// Pure parsing behind the dashboard CSV import (components/dashboard/CsvImport):
// bank export text -> cell grid -> the rows POST /api/import receives.
// Deliberately simple: no real CSV quoting (a separator inside quotes still
// splits the cell) and Chilean number and date formats only.

/** A transaction row ready for POST /api/import. */
export interface ParsedRow {
  description: string;
  amount: number;
  date: string;
}

/** Column index of each field in the data rows. */
export interface ColumnMapping {
  description: number;
  amount: number;
  date: number;
}

/** Positional mapping used when a header is not recognized. */
export const DEFAULT_MAPPING: Readonly<ColumnMapping> = {
  description: 0,
  amount: 1,
  date: 2,
};

// Lowercased header names recognized per field; the leftmost matching header
// wins.
const HEADER_KEYWORDS: Record<keyof ColumnMapping, string[]> = {
  description: ["descripcion", "description", "detalle", "glosa", "concepto"],
  amount: ["monto", "amount", "valor", "cargo", "abono"],
  date: ["fecha", "date", "dia"],
};

/**
 * Splits CSV text into trimmed, non-blank rows of cells, header included.
 * The separator is `;` when the first line contains one, else `,`; each cell
 * loses a leading and/or trailing double quote, then is trimmed.
 */
export function splitCsvRows(text: string): string[][] {
  const lines = text
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l);
  if (lines.length === 0) return [];

  const sep = lines[0].includes(";") ? ";" : ",";
  return lines.map((line) =>
    line.split(sep).map((cell) => cell.replace(/^"|"$/g, "").trim())
  );
}

/** Maps each field to the first header matching its keywords (case-insensitive),
 *  falling back to DEFAULT_MAPPING. */
export function detectColumns(headers: string[]): ColumnMapping {
  const h = headers.map((c) => c.toLowerCase());
  const find = (field: keyof ColumnMapping) => {
    const idx = h.findIndex((c) => HEADER_KEYWORDS[field].includes(c));
    return idx >= 0 ? idx : DEFAULT_MAPPING[field];
  };
  return {
    description: find("description"),
    amount: find("amount"),
    date: find("date"),
  };
}

/**
 * Parses a Chilean-formatted amount (`.` thousands, `,` decimals, e.g.
 * `1.234` or `-1.234,56`) into a rounded integer; 0 when unreadable.
 */
export function parseAmount(raw: string): number {
  const amountStr = raw
    .replace(/\./g, "")
    .replace(",", ".")
    .replace(/[^0-9.\-]/g, "");
  return Math.round(parseFloat(amountStr) || 0);
}

/** Rewrites DD/MM/YYYY or DD-MM-YYYY as YYYY-MM-DD, and a leading
 *  YYYY/MM/DD or YYYY-MM-DD (dropping any trailing time) as YYYY-MM-DD;
 *  any other value is returned unchanged. */
export function parseDate(raw: string): string {
  const ddmmyyyy = raw.match(/(\d{1,2})[/-](\d{1,2})[/-](\d{4})/);
  if (ddmmyyyy) {
    return `${ddmmyyyy[3]}-${ddmmyyyy[2].padStart(2, "0")}-${ddmmyyyy[1].padStart(2, "0")}`;
  }
  const yyyymmdd = raw.match(/^(\d{4})[/-](\d{1,2})[/-](\d{1,2})/);
  if (yyyymmdd) {
    return `${yyyymmdd[1]}-${yyyymmdd[2].padStart(2, "0")}-${yyyymmdd[3].padStart(2, "0")}`;
  }
  return raw;
}

/** Reads each data row through the mapping, dropping rows with no
 *  description, no date or a zero amount. */
export function toParsedRows(
  rows: string[][],
  mapping: ColumnMapping
): ParsedRow[] {
  return rows
    .map((row) => ({
      description: row[mapping.description] || "",
      amount: parseAmount(row[mapping.amount] || "0"),
      date: parseDate(row[mapping.date] || ""),
    }))
    .filter((r) => r.description && r.amount !== 0 && r.date);
}
