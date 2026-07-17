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
import { Button } from "@/components/ui/button";
import { formatCLP, cn } from "@/lib/utils";
import { AlertTriangle, Building2, ExternalLink, RefreshCw } from "lucide-react";

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
  externalRef: string | null;
  attributes: Record<string, unknown>;
  metrics: Record<string, unknown> | null;
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

/** Slugs with a live scraper the refresh button can trigger. Everything else
 *  (e.g. `bci_lider`, `manual`) gets a disabled button — see build_scrapers(). */
const SCRAPER_SLUGS = new Set([
  "fintual",
  "buda",
  "banchile",
  "mach",
  "mercadopago",
  "tenpo",
]);

// Latest scraper run per institution, from GET /api/scrapers (used for polling).
interface ScraperRun {
  institution: string;
  status: string;
  started_at: string;
}

const POLL_INTERVAL_MS = 3000;
const POLL_TIMEOUT_MS = 60000;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Latest run per institution (slug → status + started_at). `/api/scrapers`
 *  already returns the latest per (method, institution); collapse methods by
 *  keeping the most recent started_at so an institution maps to one entry. */
async function fetchRunMap(): Promise<Map<string, ScraperRun>> {
  const map = new Map<string, ScraperRun>();
  try {
    const res = await fetch("/api/scrapers");
    const runs: ScraperRun[] = await res.json();
    for (const r of runs) {
      const prev = map.get(r.institution);
      if (!prev || r.started_at > prev.started_at) map.set(r.institution, r);
    }
  } catch {
    /* treat as no data */
  }
  return map;
}

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

/** The product's credit limit (cupo) as observed in its latest metrics. */
function productLimit(product: Product): number | null {
  const limit = product.metrics?.limit;
  return typeof limit === "number" ? limit : null;
}

/** Human-readable detail chips pulled from the product's attributes + account. */
function productDetailChips(product: Product): string[] {
  const chips: string[] = [];
  const d = product.attributes ?? {};

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
  // Institution slugs with a scrape currently in flight (spinning buttons).
  const [syncing, setSyncing] = useState<Set<string>>(new Set());
  // Set when the scraper service is unreachable / not configured (proxy 503).
  const [serviceError, setServiceError] = useState<string | null>(null);

  async function loadInstitutions() {
    try {
      const res = await fetch("/api/institutions");
      if (!res.ok) throw new Error("failed");
      const data: ApiResponse = await res.json();
      setInstitutions(data.institutions);
      setTotals(data.totals);
    } catch {
      setError(true);
    }
  }

  useEffect(() => {
    loadInstitutions();
  }, []);

  const markDone = (slug: string) =>
    setSyncing((prev) => {
      const next = new Set(prev);
      next.delete(slug);
      return next;
    });

  /**
   * Poll `/api/scrapers` until each triggered institution's run finishes (a
   * *new* run appears — started_at past its baseline — and leaves `running`),
   * reloading balances as each one lands. Caps at POLL_TIMEOUT_MS so a slow or
   * unconfigured scraper can't spin forever.
   */
  async function pollUntilDone(pending: Set<string>, baseline: Map<string, ScraperRun>) {
    const deadline = Date.now() + POLL_TIMEOUT_MS;
    while (pending.size > 0 && Date.now() < deadline) {
      await sleep(POLL_INTERVAL_MS);
      const runs = await fetchRunMap();
      let anyDone = false;
      for (const slug of [...pending]) {
        const run = runs.get(slug);
        if (!run) continue;
        const base = baseline.get(slug);
        const isNewRun = !base || run.started_at > base.started_at;
        if (isNewRun && run.status !== "running") {
          pending.delete(slug);
          markDone(slug);
          anyDone = true;
        }
      }
      if (anyDone) await loadInstitutions();
    }
    // Timed out with runs still pending: stop spinning and show latest data.
    if (pending.size > 0) {
      for (const slug of pending) markDone(slug);
      await loadInstitutions();
    }
  }

  /** Trigger a scrape for one institution (by slug) or all when omitted. */
  async function refresh(slug?: string) {
    setServiceError(null);
    // Snapshot current runs first so polling can tell the new run apart.
    const baseline = await fetchRunMap();

    let triggered: string[];
    try {
      const res = await fetch("/api/institutions/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(slug ? { institution: slug } : {}),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setServiceError(
          data.error ??
            (res.status === 503
              ? "Servicio de scrapers no disponible."
              : "No se pudo iniciar la sincronización.")
        );
        return;
      }
      triggered =
        Array.isArray(data.triggered) && data.triggered.length > 0
          ? data.triggered
          : slug
            ? [slug]
            : [];
    } catch {
      setServiceError("Servicio de scrapers no disponible.");
      return;
    }

    if (triggered.length === 0) return;
    setSyncing((prev) => new Set([...prev, ...triggered]));
    void pollUntilDone(new Set(triggered), baseline);
  }

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
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-2xl font-bold">Instituciones</h2>
          <p className="text-sm text-muted-foreground">
            Cada banco, fintech y exchange que sigues, con el detalle de sus
            productos.
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => refresh()}
          disabled={syncing.size > 0}
        >
          <RefreshCw
            className={cn("h-4 w-4", syncing.size > 0 && "animate-spin")}
          />
          {syncing.size > 0 ? "Sincronizando…" : "Actualizar todo"}
        </Button>
      </div>

      {serviceError && (
        <div className="flex items-start gap-3 rounded-md border border-red-200 bg-red-50 p-3 dark:border-red-900 dark:bg-red-950">
          <AlertTriangle className="mt-0.5 h-4 w-4 text-red-600" />
          <p className="text-sm text-red-800 dark:text-red-200">
            {serviceError}
          </p>
        </div>
      )}

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
                <div className="flex items-center gap-2">
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
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    onClick={() => refresh(inst.slug)}
                    disabled={
                      !SCRAPER_SLUGS.has(inst.slug) || syncing.has(inst.slug)
                    }
                    title={
                      SCRAPER_SLUGS.has(inst.slug)
                        ? "Actualizar"
                        : "Sin scraper disponible"
                    }
                  >
                    <RefreshCw
                      className={cn(
                        "h-4 w-4",
                        syncing.has(inst.slug) && "animate-spin"
                      )}
                    />
                  </Button>
                </div>
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
                    const cupo = productLimit(product);
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
                          {cupo != null ? formatCLP(cupo) : "—"}
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
