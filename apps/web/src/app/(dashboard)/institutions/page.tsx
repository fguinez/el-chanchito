"use client";

import { useEffect, useState } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { formatCLP, cn } from "@/lib/utils";
import { Building2, ExternalLink } from "lucide-react";

interface Product {
  id: string;
  accountId: string;
  accountName: string;
  parentProductId: string | null;
  kind: string;
  name: string;
  currency: string;
  currentBalance: number | null;
  currentBalanceClp: number | null;
  balanceAsOf: string | null;
  creditLimit: number | null;
  externalRef: string | null;
  details: Record<string, unknown>;
  isActive: boolean;
}

interface Subtotals {
  byCurrency: { currency: string; amount: number }[];
  clp: number | null;
  patrimonioClp: number | null;
  deudaClp: number | null;
  convertible: boolean;
}

interface Institution {
  id: string;
  slug: string;
  name: string;
  kind: string;
  country: string | null;
  url: string | null;
  products: Product[];
  subtotals: Subtotals;
}

interface Totals {
  patrimonioClp: number;
  deudaClp: number;
  netClp: number;
}

interface ApiResponse {
  institutions: Institution[];
  totals: Totals;
}

const INSTITUTION_KIND_LABELS: Record<string, string> = {
  bank: "Banco",
  fintech: "Fintech",
  exchange: "Exchange",
  asset_manager: "Gestora",
  other: "Otro",
};

const PRODUCT_KIND_LABELS: Record<string, string> = {
  checking: "Cuenta corriente",
  savings: "Ahorro",
  vista: "Cuenta vista",
  wallet: "Billetera",
  term_deposit: "Depósito a plazo",
  credit_card: "Tarjeta de crédito",
  debit_card: "Tarjeta de débito",
  prepaid_card: "Tarjeta prepago",
  line_of_credit: "Línea de crédito",
  loan: "Préstamo",
  mortgage: "Hipotecario",
  investment: "Inversión",
  crypto: "Cripto",
  other: "Otro",
};

const LIABILITY_KINDS = new Set([
  "credit_card",
  "line_of_credit",
  "loan",
  "mortgage",
]);

function timeAgo(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return "recién";
  if (minutes < 60) return `hace ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `hace ${hours}h`;
  const days = Math.floor(hours / 24);
  return `hace ${days}d`;
}

/** Format a balance in its own currency: CLP as pesos, anything else (crypto,
 *  foreign) as a trimmed decimal followed by the currency code. */
function formatBalance(currency: string, value: number | null): string | null {
  if (value == null) return null;
  if (currency === "CLP") return formatCLP(value);
  const formatted = new Intl.NumberFormat("en-US", {
    maximumFractionDigits: 8,
  }).format(value);
  return `${formatted} ${currency}`;
}

/**
 * A meaningful label for the Producto column. Scraped products are auto-named
 * "Institution - kind" (e.g. "Buda - crypto (ETH)"), so a raw name carries no
 * more information than the Tipo badge. When that's the case we fall back to
 * the currency for crypto (CLP / ETH / BTC …) and to the friendly kind label
 * otherwise; anything a human named stays untouched.
 */
function displayProductName(product: Product, institutionName: string): string {
  const prefix = `${institutionName} - `;
  const cleaned = (
    product.name.startsWith(prefix)
      ? product.name.slice(prefix.length)
      : product.name
  ).trim();

  const isGeneric =
    cleaned === product.kind || cleaned.startsWith(`${product.kind} (`);
  if (isGeneric || !cleaned) {
    if (product.kind === "crypto") return product.currency;
    return PRODUCT_KIND_LABELS[product.kind] ?? product.kind;
  }
  return cleaned;
}

/** Human-readable detail chips pulled from the product's details JSONB + account. */
function productDetailChips(product: Product): string[] {
  const chips: string[] = [];
  const d = product.details ?? {};

  if (product.accountName && product.accountName !== "Personal") {
    chips.push(product.accountName);
  }
  if (typeof d.brand === "string") chips.push(d.brand);
  if (typeof d.last4 === "string") chips.push(`•••• ${d.last4}`);
  if (typeof d.portfolio === "string") chips.push(d.portfolio);
  if (typeof d.riskProfile === "string") chips.push(d.riskProfile);
  if (typeof d.statementDay === "number")
    chips.push(`corte día ${d.statementDay}`);
  if (typeof d.dueDay === "number") chips.push(`vence día ${d.dueDay}`);

  return chips;
}

export default function InstitutionsPage() {
  const [institutions, setInstitutions] = useState<Institution[] | null>(null);
  const [totals, setTotals] = useState<Totals | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    fetch("/api/institutions")
      .then((res) => {
        if (!res.ok) throw new Error("failed");
        return res.json();
      })
      .then((data: ApiResponse) => {
        setInstitutions(data.institutions);
        setTotals(data.totals);
      })
      .catch(() => setError(true));
  }, []);

  if (error) {
    return (
      <div className="space-y-6">
        <h2 className="text-2xl font-bold">Instituciones</h2>
        <p className="text-muted-foreground">
          No se pudieron cargar las instituciones.
        </p>
      </div>
    );
  }

  if (!institutions) {
    return (
      <div className="space-y-6">
        <h2 className="text-2xl font-bold">Instituciones</h2>
        <p className="text-muted-foreground">Cargando...</p>
      </div>
    );
  }

  const totalProducts = institutions.reduce(
    (sum, inst) => sum + inst.products.length,
    0
  );
  const lastUpdated = institutions
    .flatMap((inst) => inst.products)
    .map((p) => p.balanceAsOf)
    .filter((v): v is string => v != null)
    .sort()
    .at(-1);

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold">Instituciones</h2>
        <p className="text-sm text-muted-foreground">
          Cada banco, fintech y exchange que sigues, con el detalle de sus
          productos.
        </p>
      </div>

      {/* Summary */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Instituciones</CardDescription>
            <CardTitle className="text-2xl">{institutions.length}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Productos</CardDescription>
            <CardTitle className="text-2xl">{totalProducts}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Patrimonio neto (CLP)</CardDescription>
            <CardTitle className="text-2xl">
              {totals ? formatCLP(totals.netClp) : "—"}
            </CardTitle>
            {totals && totals.deudaClp > 0 && (
              <p className="text-xs text-muted-foreground">
                deuda {formatCLP(totals.deudaClp)}
              </p>
            )}
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Última actualización</CardDescription>
            <CardTitle className="text-2xl">
              {lastUpdated ? timeAgo(lastUpdated) : "—"}
            </CardTitle>
          </CardHeader>
        </Card>
      </div>

      {institutions.length === 0 ? (
        <Card>
          <CardContent className="pt-6">
            <p className="text-center text-muted-foreground">
              Todavía no hay productos. Cuando los scrapers importen datos,
              cada institución aparecerá aquí con sus cuentas y saldos.
            </p>
          </CardContent>
        </Card>
      ) : (
        institutions.map((inst) => (
          <Card key={inst.id}>
            <CardHeader>
              <div className="flex items-start justify-between gap-3">
                <div className="flex items-center gap-3">
                  <div className="flex h-9 w-9 items-center justify-center rounded-md bg-muted">
                    <Building2 className="h-5 w-5 text-muted-foreground" />
                  </div>
                  <div>
                    <CardTitle className="flex items-center gap-2 text-lg">
                      {inst.name}
                      <Badge variant="secondary" className="text-xs">
                        {INSTITUTION_KIND_LABELS[inst.kind] ?? inst.kind}
                      </Badge>
                    </CardTitle>
                    <CardDescription>
                      {inst.products.length}{" "}
                      {inst.products.length === 1 ? "producto" : "productos"}
                      {inst.country ? ` · ${inst.country}` : ""}
                    </CardDescription>
                  </div>
                </div>
                {inst.url && (
                  <a
                    href={inst.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
                  >
                    Sitio <ExternalLink className="h-3.5 w-3.5" />
                  </a>
                )}
              </div>
            </CardHeader>
            <CardContent>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Producto</TableHead>
                    <TableHead>Tipo</TableHead>
                    <TableHead className="text-right">Saldo</TableHead>
                    <TableHead className="text-right">Cupo</TableHead>
                    <TableHead className="text-right">Actualizado</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {inst.products.map((product) => {
                    const chips = productDetailChips(product);
                    const balance = formatBalance(
                      product.currency,
                      product.currentBalance
                    );
                    const isLiability = LIABILITY_KINDS.has(product.kind);
                    return (
                      <TableRow
                        key={product.id}
                        className={cn(!product.isActive && "opacity-50")}
                      >
                        <TableCell>
                          <div className="font-medium">
                            {displayProductName(product, inst.name)}
                          </div>
                          {chips.length > 0 && (
                            <div className="mt-0.5 text-xs text-muted-foreground">
                              {chips.join(" · ")}
                            </div>
                          )}
                        </TableCell>
                        <TableCell>
                          <Badge
                            variant="outline"
                            className={cn(
                              "text-xs",
                              isLiability
                                ? "border-red-200 text-red-600"
                                : "border-green-200 text-green-700"
                            )}
                          >
                            {PRODUCT_KIND_LABELS[product.kind] ?? product.kind}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-right font-medium tabular-nums">
                          {balance ?? (
                            <span className="text-muted-foreground">
                              sin dato
                            </span>
                          )}
                          {product.currency !== "CLP" &&
                            product.currentBalanceClp != null && (
                              <div className="text-xs font-normal text-muted-foreground">
                                ≈ {formatCLP(product.currentBalanceClp)}
                              </div>
                            )}
                        </TableCell>
                        <TableCell className="text-right tabular-nums text-muted-foreground">
                          {product.creditLimit != null
                            ? formatCLP(product.creditLimit)
                            : "—"}
                        </TableCell>
                        <TableCell className="text-right text-sm text-muted-foreground">
                          {product.balanceAsOf
                            ? timeAgo(product.balanceAsOf)
                            : "—"}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>

              {/* Per-institution subtotals: holdings by currency + CLP total */}
              {inst.subtotals.clp != null && (
                <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t pt-3 text-sm">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <span className="font-medium">Saldos por moneda:</span>
                    {inst.subtotals.byCurrency.length > 0 ? (
                      inst.subtotals.byCurrency.map((s) => (
                        <Badge
                          key={s.currency}
                          variant="secondary"
                          className="font-normal tabular-nums"
                        >
                          {formatBalance(s.currency, s.amount)}
                        </Badge>
                      ))
                    ) : (
                      <span className="text-muted-foreground">sin saldos</span>
                    )}
                  </div>
                  <div className="text-right">
                    <span className="text-muted-foreground">Total en CLP </span>
                    <span
                      className={cn(
                        "font-semibold tabular-nums",
                        inst.subtotals.clp < 0 && "text-red-600"
                      )}
                    >
                      ≈ {formatCLP(inst.subtotals.clp)}
                    </span>
                    {!inst.subtotals.convertible && (
                      <span className="ml-1 text-xs text-muted-foreground">
                        (algunos saldos sin conversión)
                      </span>
                    )}
                  </div>
                </div>
              )}
            </CardContent>
          </Card>
        ))
      )}
    </div>
  );
}
