// "Tipo de cambio" card of the product detail page: the CLP rate the
// product's figures convert with, where it comes from, when it was read and
// whether it is stale. An institution's own rate is reread by the institution
// page's "Actualizar" (the scraper reruns as a whole).

import { AlertTriangle } from "lucide-react";
import {
  Card,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type { RateInfo } from "@/lib/rates";
import { formatClpRate, formatPlainDateEs } from "@/lib/utils";
import { timeAgo } from "./shared";

export function ExchangeRateCard({
  currency,
  rate,
  institution,
}: {
  /** The product currency (the rate's base). */
  currency: string;
  rate: RateInfo | null;
  institution: { slug: string; name: string };
}) {
  let origin: string;
  if (rate == null) origin = `Ninguna fuente cotiza ${currency}`;
  else if (rate.scope === "provider") origin = "Referencial de mercado";
  else origin = [institution.name, rate.source].filter(Boolean).join(" · ");

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardDescription>Tipo de cambio</CardDescription>
        <CardTitle className="text-2xl tabular-nums">
          {rate != null ? (
            <>
              {formatClpRate(rate.rate)}{" "}
              <span className="text-sm font-normal text-muted-foreground">
                por {rate.currency}
              </span>
            </>
          ) : (
            <span className="text-muted-foreground">sin dato</span>
          )}
        </CardTitle>
        <div className="space-y-0.5 text-xs text-muted-foreground">
          <p>{origin}</p>
          {rate?.observedAt && (
            <p>
              Leído {timeAgo(rate.observedAt)}
              {rate.reportedAsOf &&
                ` · vigente al ${formatPlainDateEs(rate.reportedAsOf)}`}
            </p>
          )}
          {rate?.stale && (
            <p className="flex items-center gap-1 font-medium text-amber-600">
              <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
              Desactualizado: no es de hoy
            </p>
          )}
        </div>
      </CardHeader>
    </Card>
  );
}
