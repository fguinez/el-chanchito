"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  InteractiveChart,
  useTimeSeriesChart,
} from "@/components/charts/interactive-chart";
import { TimeRangeControl } from "@/components/charts/time-range-control";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn, formatCLP, formatDateEs } from "@/lib/utils";
import { ArrowLeft } from "lucide-react";
import { KIND_INFO } from "@chanchito/product-model";
import {
  formatBalance,
  productDetailChips,
  productLimit,
  displayProductName,
  type InstitutionProduct,
  type ProductParent,
} from "@/components/institutions/shared";
import { ProductEditDialog } from "@/components/institutions/product-edit-dialog";
import {
  CupoUtilizationCard,
  InvestmentCompositionCard,
  MonthlyFlowCard,
  TransactionWaterfallCard,
  TRANSACTION_KINDS,
  type ProductHistoryPoint,
  type ProductTransaction,
} from "@/components/institutions/product-graphs";
import { BalanceHistorySheet } from "@/components/institutions/balance-history-sheet";
import { ExchangeRateCard } from "@/components/institutions/exchange-rate-card";
import { ProductTransactionsCard } from "@/components/institutions/product-transactions";

interface ProductDetailResponse {
  institution: {
    slug: string;
    name: string;
    kind: string;
  };
  product: InstitutionProduct;
  parent: ProductParent | null;
  history: ProductHistoryPoint[];
  transactions: ProductTransaction[];
  transactionsTruncated: boolean;
}

/** Recharts rows from the balance history, own-currency balance. */
function buildChartData(history: ProductHistoryPoint[]) {
  return history.map((p) => ({
    t: new Date(p.asOf).getTime(),
    Saldo: p.balance,
  }));
}

export default function ProductDetailPage() {
  const { institution: institutionSlug, product: productSlug } = useParams<{
    institution: string;
    product: string;
  }>();
  const router = useRouter();

  const [data, setData] = useState<ProductDetailResponse | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState(false);
  // Bumped to re-fetch in place (e.g. after an edit that keeps the URL).
  const [reloadKey, setReloadKey] = useState(0);
  const chart = useTimeSeriesChart();
  const productUrl = `/api/institutions/${institutionSlug}/products/${productSlug}`;

  useEffect(() => {
    let cancelled = false;
    fetch(productUrl)
      .then((res) => {
        if (cancelled) return null;
        if (res.status === 404) {
          setNotFound(true);
          setError(false);
          setData(null);
          return null;
        }
        if (!res.ok) throw new Error("failed");
        return res.json();
      })
      .then((body) => {
        if (cancelled || !body) return;
        setData(body);
        setNotFound(false);
        setError(false);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      });
    return () => {
      cancelled = true;
    };
  }, [productUrl, reloadKey]);

  /** A new slug moves the page (the effect refetches on the new params). */
  function handleSaved(slug: string) {
    if (slug !== productSlug) {
      router.replace(`/institutions/${institutionSlug}/${slug}`);
    } else {
      setReloadKey((k) => k + 1);
    }
  }

  if (notFound) {
    return (
      <div className="space-y-6">
        <h2 className="text-2xl font-bold">Producto</h2>
        <p className="text-muted-foreground">Producto no encontrado.</p>
        <Button asChild variant="outline" size="sm">
          <Link href={`/institutions/${institutionSlug}`}>
            Volver a la institución
          </Link>
        </Button>
      </div>
    );
  }
  if (error && !data) {
    return (
      <div className="space-y-6">
        <h2 className="text-2xl font-bold">Producto</h2>
        <p className="text-muted-foreground">No se pudo cargar el producto.</p>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="space-y-6">
        <h2 className="text-2xl font-bold">Producto</h2>
        <p className="text-muted-foreground">Cargando...</p>
      </div>
    );
  }

  const {
    institution,
    product,
    parent,
    history,
    transactions,
    transactionsTruncated,
  } = data;
  const isLiability = KIND_INFO[product.kind].role === "liability";
  const chips = productDetailChips(product);
  const cupo = productLimit(product);
  const chartData = buildChartData(history);
  const latestBalance = history.at(-1)?.balance ?? null;
  const hasHistory = chartData.some((p) => p.Saldo != null);

  const isCreditKind =
    product.kind === "credit_card" || product.kind === "line_of_credit";
  const anchorBalance = product.currentBalance ?? latestBalance;
  const showRate = product.currency !== "CLP";

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-1.5 text-sm text-muted-foreground">
        <Link
          href="/institutions"
          className="flex items-center gap-1 hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          Instituciones
        </Link>
        <span>/</span>
        <Link
          href={`/institutions/${institution.slug}`}
          className="hover:text-foreground"
        >
          {institution.name}
        </Link>
        <span>/</span>
        <span className="text-foreground">
          {displayProductName(product, institution.name)}
        </span>
      </div>

      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="flex flex-wrap items-center gap-2 text-2xl font-bold">
            {displayProductName(product, institution.name)}
            <Badge
              variant="outline"
              className={cn(
                "text-xs",
                isLiability
                  ? "border-red-200 text-red-600"
                  : "border-green-200 text-green-700"
              )}
            >
              {KIND_INFO[product.kind].labelEs}
            </Badge>
            {!product.isActive && <Badge variant="outline">Inactivo</Badge>}
            <Badge variant="secondary">{product.currency}</Badge>
          </h2>
          {chips.length > 0 && (
            <p className="mt-1 text-sm text-muted-foreground">{chips.join(" · ")}</p>
          )}
          {parent && (
            <p className="mt-1 text-sm text-muted-foreground">
              Vinculado a{" "}
              <Link
                href={`/institutions/${parent.institutionSlug}/${parent.slug}`}
                className="font-medium text-foreground hover:text-primary"
              >
                {displayProductName(parent, institution.name)}
              </Link>
            </p>
          )}
        </div>
        <ProductEditDialog
          institution={institution}
          product={product}
          onSaved={handleSaved}
        />
      </div>

      {/* Current state */}
      <div
        className={cn(
          "grid gap-4 md:grid-cols-2",
          showRate ? "lg:grid-cols-4" : "lg:grid-cols-3"
        )}
      >
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Saldo actual</CardDescription>
            <CardTitle className="text-2xl tabular-nums">
              {formatBalance(product.currency, product.currentBalance) ?? (
                <span className="text-muted-foreground">sin dato</span>
              )}
            </CardTitle>
            {product.currency !== "CLP" && product.currentBalanceClp != null && (
              <p className="text-xs text-muted-foreground">
                ≈ {formatCLP(product.currentBalanceClp)}
              </p>
            )}
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Cupo</CardDescription>
            <CardTitle className="text-2xl tabular-nums">
              {cupo != null ? (
                formatBalance(product.currency, cupo)
              ) : (
                <span className="text-muted-foreground">—</span>
              )}
            </CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Actualizado</CardDescription>
            <CardTitle className="text-2xl">
              {product.balanceAsOf ? (
                formatDateEs(product.balanceAsOf)
              ) : (
                <span className="text-muted-foreground">—</span>
              )}
            </CardTitle>
            {product.balanceAsOf && (
              <p className="text-xs text-muted-foreground">
                {product.balanceAsOf
                  ? new Date(product.balanceAsOf).toLocaleTimeString("es-CL", {
                      hour: "2-digit",
                      minute: "2-digit",
                    })
                  : ""}
              </p>
            )}
          </CardHeader>
        </Card>
        {showRate && (
          <ExchangeRateCard
            currency={product.currency}
            rate={product.exchangeRate}
            institution={institution}
          />
        )}
      </div>

      {/* Per-kind insight graphs */}
      {isCreditKind && <CupoUtilizationCard product={product} history={history} />}
      {product.kind === "investment" && (
        <InvestmentCompositionCard product={product} />
      )}
      {TRANSACTION_KINDS.has(product.kind) && (
        <TransactionWaterfallCard
          product={product}
          transactions={transactions}
          currentBalance={anchorBalance}
        />
      )}
      {TRANSACTION_KINDS.has(product.kind) && transactions.length >= 2 && (
        <MonthlyFlowCard product={product} transactions={transactions} />
      )}

      {/* Balance history */}
      <Card>
        <CardHeader>
          <CardTitle>Historial de saldo</CardTitle>
          <CardDescription>
            Evolución del saldo según cada cambio reportado
          </CardDescription>
          <CardAction className="flex flex-wrap items-center justify-end gap-2">
            <TimeRangeControl control={chart.x} allowAll />
            {history.length > 0 && (
              <BalanceHistorySheet
                currency={product.currency}
                history={history}
              />
            )}
          </CardAction>
        </CardHeader>
        <CardContent>
          {hasHistory ? (
            <InteractiveChart {...chart.interactiveProps} height={300}>
              <LineChart data={chartData}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis {...chart.xAxisProps} />
                <YAxis {...chart.yAxisProps} />
                <Tooltip
                  formatter={(value) =>
                    formatBalance(product.currency, Number(value)) ?? "—"
                  }
                  labelFormatter={chart.labelFormatter}
                />
                <Line
                  type="monotone"
                  dataKey="Saldo"
                  stroke="#2563eb"
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                />
              </LineChart>
            </InteractiveChart>
          ) : (
            <p className="text-muted-foreground">Sin historial de saldo.</p>
          )}
        </CardContent>
      </Card>

      <ProductTransactionsCard
        key={product.id}
        currency={product.currency}
        transactions={transactions}
        truncated={transactionsTruncated}
        onTransferChanged={() => setReloadKey((k) => k + 1)}
      />
    </div>
  );
}
