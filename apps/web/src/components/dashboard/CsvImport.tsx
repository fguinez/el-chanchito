"use client";

import { useState, useRef } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Upload } from "lucide-react";

interface ParsedRow {
  description: string;
  amount: number;
  date: string;
}

interface ColumnMapping {
  description: number;
  amount: number;
  date: number;
}

export function CsvImport({ onImported }: { onImported: () => void }) {
  const [csvData, setCsvData] = useState<string[][] | null>(null);
  const [headers, setHeaders] = useState<string[]>([]);
  const [mapping, setMapping] = useState<ColumnMapping>({
    description: 0,
    amount: 1,
    date: 2,
  });
  const [institution, setInstitution] = useState("csv_import");
  const [importing, setImporting] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const handleFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = (evt) => {
      const text = evt.target?.result as string;
      const lines = text
        .split("\n")
        .map((l) => l.trim())
        .filter((l) => l);

      if (lines.length < 2) return;

      // Detect separator
      const sep = lines[0].includes(";") ? ";" : ",";
      const parsed = lines.map((line) =>
        line.split(sep).map((cell) => cell.replace(/^"|"$/g, "").trim())
      );

      setHeaders(parsed[0]);
      setCsvData(parsed.slice(1));

      // Auto-detect column mapping
      const h = parsed[0].map((h) => h.toLowerCase());
      const descIdx = h.findIndex((c) =>
        ["descripcion", "description", "detalle", "glosa", "concepto"].includes(c)
      );
      const amtIdx = h.findIndex((c) =>
        ["monto", "amount", "valor", "cargo", "abono"].includes(c)
      );
      const dateIdx = h.findIndex((c) =>
        ["fecha", "date", "dia"].includes(c)
      );

      setMapping({
        description: descIdx >= 0 ? descIdx : 0,
        amount: amtIdx >= 0 ? amtIdx : 1,
        date: dateIdx >= 0 ? dateIdx : 2,
      });

      setResult(null);
    };
    reader.readAsText(file);
  };

  const parseRows = (): ParsedRow[] => {
    if (!csvData) return [];

    return csvData
      .map((row) => {
        const desc = row[mapping.description] || "";
        const rawAmount = row[mapping.amount] || "0";
        const rawDate = row[mapping.date] || "";

        // Parse amount: handle "1.234" (CLP thousands) and "-1.234,56"
        const amountStr = rawAmount
          .replace(/\./g, "")
          .replace(",", ".")
          .replace(/[^0-9.\-]/g, "");
        const amount = parseFloat(amountStr) || 0;

        // Parse date: try common Chilean formats
        let dateStr = rawDate;
        // DD/MM/YYYY or DD-MM-YYYY -> YYYY-MM-DD
        const ddmmyyyy = rawDate.match(/(\d{1,2})[/-](\d{1,2})[/-](\d{4})/);
        if (ddmmyyyy) {
          dateStr = `${ddmmyyyy[3]}-${ddmmyyyy[2].padStart(2, "0")}-${ddmmyyyy[1].padStart(2, "0")}`;
        }

        return { description: desc, amount: Math.round(amount), date: dateStr };
      })
      .filter((r) => r.description && r.amount !== 0 && r.date);
  };

  const handleImport = async () => {
    const rows = parseRows();
    if (rows.length === 0) return;

    setImporting(true);
    setResult(null);

    try {
      const res = await fetch("/api/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          rows,
          institution,
          kind: "checking",
        }),
      });

      const data = await res.json();
      setResult(
        `Importados: ${data.imported}, Omitidos: ${data.skipped}, Total: ${data.total}`
      );
      onImported();
    } catch {
      setResult("Error al importar");
    } finally {
      setImporting(false);
    }
  };

  const preview = csvData ? parseRows().slice(0, 5) : [];

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Upload className="h-4 w-4" />
          Importar CSV
        </CardTitle>
        <CardDescription>
          Importa transacciones desde un archivo CSV de cualquier banco
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="space-y-4">
          {/* File picker */}
          <div className="flex items-center gap-3">
            <Input
              ref={fileRef}
              type="file"
              accept=".csv,.txt"
              onChange={handleFile}
              className="max-w-xs"
            />
            <div className="w-40">
              <Input
                value={institution}
                onChange={(e) => setInstitution(e.target.value)}
                placeholder="Institucion"
              />
            </div>
          </div>

          {/* Column mapping */}
          {headers.length > 0 && (
            <div className="space-y-2">
              <p className="text-sm font-medium">Mapeo de columnas</p>
              <div className="flex gap-4 text-sm">
                {(["description", "amount", "date"] as const).map((field) => (
                  <div key={field}>
                    <label className="mb-1 block text-muted-foreground">
                      {field === "description"
                        ? "Descripcion"
                        : field === "amount"
                          ? "Monto"
                          : "Fecha"}
                    </label>
                    <select
                      value={mapping[field]}
                      onChange={(e) =>
                        setMapping((m) => ({
                          ...m,
                          [field]: parseInt(e.target.value),
                        }))
                      }
                      className="rounded border bg-background px-2 py-1 text-sm"
                    >
                      {headers.map((h, i) => (
                        <option key={i} value={i}>
                          {h}
                        </option>
                      ))}
                    </select>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Preview */}
          {preview.length > 0 && (
            <div className="space-y-2">
              <p className="text-sm font-medium">
                Vista previa ({csvData?.length} filas)
              </p>
              <div className="overflow-x-auto rounded border">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b bg-muted/50">
                      <th className="p-2 text-left">Descripcion</th>
                      <th className="p-2 text-right">Monto</th>
                      <th className="p-2 text-left">Fecha</th>
                    </tr>
                  </thead>
                  <tbody>
                    {preview.map((row, i) => (
                      <tr key={i} className="border-b">
                        <td className="p-2">{row.description}</td>
                        <td
                          className={`p-2 text-right ${row.amount < 0 ? "text-red-600" : "text-green-600"}`}
                        >
                          ${Math.abs(row.amount).toLocaleString("es-CL")}
                        </td>
                        <td className="p-2">{row.date}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Import button */}
          {csvData && (
            <div className="flex items-center gap-3">
              <Button onClick={handleImport} disabled={importing}>
                {importing
                  ? "Importando..."
                  : `Importar ${parseRows().length} transacciones`}
              </Button>
              {result && (
                <span className="text-sm text-muted-foreground">{result}</span>
              )}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
