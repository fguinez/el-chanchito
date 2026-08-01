"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
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
import { cn, formatCLP, formatAxisValue, formatDateEs } from "@/lib/utils";
import { ArrowLeft } from "lucide-react";
import { KIND_INFO } from "@chanchito/product-model";
import {
  formatBalance,
  productDetailChips,
  productLimit,
  displayProductName,
  type InstitutionProduct,
} from "@/components/institutions/shared";
import {
  CupoUtilizationCard,
  InvestmentCompositionCard,
  MonthlyFlowCard,
  TransactionWaterfallCard,
  TRANSACTION_KINDS,
  type ProductHistoryPoint,
  type ProductTransaction,
} from "@/components/institutions/product-graphs";

interface ProductDetailResponse {
  institution: {
    slug: string;
    name: string;
    kind: string;
  };
  product: InstitutionProduct;
  history: ProductHistoryPoint[];
  transactions: ProductTransaction[];
}

/** Recharts rows from the balance history, own-currency balance. */
function buildChartData(history: ProductHistoryPoint[]) {
  return history.map((p) => ({ date: formatDateEs(p.asOf), Saldo: p.balance }));
}

/** The chart plots at most this many points to stay readable. */
const CHART_MAX_POINTS = 180;
const HISTORY_LIST_ROWS = 30;

export default function ProductDetailPage() {
  const { institution: institutionSlug, product: productSlug } = useParams<{
    institution: string;
    product: string;
  }>();

  const [data, setData] = useState<ProductDetailResponse | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetch(`/api/institutions/${institutionSlug}/products/${productSlug}`)
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
  }, [institutionSlug, productSlug]);

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

  const { institution, product, history, transactions } = data;
  const isLiability = KIND_INFO[product.kind].role === "liability";
  const chips = productDetailChips(product);
  const cupo = productLimit(product);
  const chartData = buildChartData(history.slice(-CHART_MAX_POINTS));
  const latestHistory = history.slice(-HISTORY_LIST_ROWS).reverse();
  const hasHistory = chartData.some((p) => p.Saldo != null);

  const isCreditKind =
    product.kind === "credit_card" || product.kind === "line_of_credit";
  const showWaterfall =
    TRANSACTION_KINDS.has(product.kind) &&
    transactions.length >= 2 &&
    (product.currentBalance ?? latestHistory[0]?.balance ?? null) != null;
  const anchorBalance = product.currentBalance ?? latestHistory[0]?.balance ?? 0;

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
      </div>

      {/* Current state */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
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
                formatCLP(cupo)
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
      </div>

      {/* Per-kind insight graphs */}
      {isCreditKind && <CupoUtilizationCard product={product} history={history} />}
      {product.kind === "investment" && (
        <InvestmentCompositionCard product={product} />
      )}
      {showWaterfall && (
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
        </CardHeader>
        <CardContent>
          {hasHistory ? (
            <>
              <ResponsiveContainer width="100%" height={300}>
                <LineChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="date" fontSize={12} />
                  <YAxis tickFormatter={formatAxisValue} fontSize={12} />
                  <Tooltip
                    formatter={(value) =>
                      formatBalance(product.currency, Number(value)) ?? "—"
                    }
                  />
                  <Line
                    type="monotone"
                    dataKey="Saldo"
                    stroke="#2563eb"
                    strokeWidth={2}
                    dot={false}
                  />
                </LineChart>
              </ResponsiveContainer>
              {product.currency !== "CLP" && (
                <p className="mt-2 text-xs text-muted-foreground">
                  Aproximación: el saldo en CLP se convierte con los tipos de
                  cambio actuales.
                </p>
              )}

              <Table className="mt-6">
                <TableHeader>
                  <TableRow>
                    <TableHead>Fecha</TableHead>
                    <TableHead align="right">Saldo</TableHead>
                    {product.currency !== "CLP" && (
                      <TableHead align="right">≈ CLP</TableHead>
                    )}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {latestHistory.map((p) => (
                    <TableRow key={p.asOf}>
                      <TableCell className="text-muted-foreground">
                        {formatDateEs(p.asOf)}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {formatBalance(product.currency, p.balance) ?? "—"}
                      </TableCell>
                      {product.currency !== "CLP" && (
                        <TableCell className="text-right tabular-nums text-muted-foreground">
                          {p.balanceClp != null ? formatCLP(p.balanceClp) : "—"}
                        </TableCell>
                      )}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </>
          ) : (
            <p className="text-muted-foreground">Sin historial de saldo.</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
