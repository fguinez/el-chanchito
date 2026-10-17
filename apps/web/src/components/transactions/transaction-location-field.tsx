"use client";

import { useState, type FormEvent, type ReactNode } from "react";
import { ExternalLink, MapPin, Pencil } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  LOCATION_TEXT_MAX_LENGTH,
  formatCoordinates,
  formatLocationText,
  openStreetMapUrl,
  type TransactionLocation,
} from "@/lib/transaction-location";

const FIELD_LABELS: Record<string, string> = {
  "location.name": "Lugar",
  "location.city": "Ciudad",
  "location.country": "País",
  "location.latitude": "Latitud",
  "location.longitude": "Longitud",
};

interface Draft {
  name: string;
  city: string;
  country: string;
  latitude: string;
  longitude: string;
}

function draftOf(location: TransactionLocation | null): Draft {
  return {
    name: location?.name ?? "",
    city: location?.city ?? "",
    country: location?.country ?? "",
    latitude: location?.latitude?.toString() ?? "",
    longitude: location?.longitude?.toString() ?? "",
  };
}

/** A coordinate typed in the form: "" is none, anything else must parse. */
function parseCoordinate(text: string): number | null | undefined {
  const trimmed = text.trim().replace(",", ".");
  if (trimmed === "") return null;
  const value = Number(trimmed);
  return Number.isFinite(value) ? value : undefined;
}

/**
 * The detail panel's "Ubicación": the stored location (hidden when there is
 * none) plus a small inline form to set, edit or clear it through
 * PATCH /api/transactions/[id]. `onSaved` receives the transaction id it saved
 * and what the server stored, so a late response for another transaction can
 * be told apart.
 */
export function TransactionLocationField({
  transactionId,
  location,
  isManual,
  onSaved,
  renderRow,
}: {
  transactionId: string;
  location: TransactionLocation | null;
  isManual: boolean;
  onSaved: (
    transactionId: string,
    location: TransactionLocation | null
  ) => void;
  /** Renders a label/value row in the panel's own layout. */
  renderRow: (label: string, children: ReactNode) => ReactNode;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Draft>(() => draftOf(location));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const startEditing = () => {
    setDraft(draftOf(location));
    setError(null);
    setEditing(true);
  };

  const save = async (next: Record<string, unknown> | null) => {
    setSaving(true);
    setError(null);
    try {
      const res = await fetch(
        `/api/transactions/${encodeURIComponent(transactionId)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ location: next }),
        }
      );
      const body = await res.json().catch(() => null);
      if (!res.ok) {
        const label = body?.field ? FIELD_LABELS[body.field] : undefined;
        setError(
          label
            ? `Revisa el campo ${label}.`
            : "No se pudo guardar la ubicación."
        );
        return;
      }
      onSaved(transactionId, body.location ?? null);
      setEditing(false);
    } catch {
      setError("No se pudo guardar la ubicación.");
    } finally {
      setSaving(false);
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const latitude = parseCoordinate(draft.latitude);
    const longitude = parseCoordinate(draft.longitude);
    if (latitude === undefined || longitude === undefined) {
      setError("Latitud y longitud deben ser números.");
      return;
    }
    if ((latitude === null) !== (longitude === null)) {
      setError("Ingresa latitud y longitud juntas, o ninguna.");
      return;
    }
    void save({
      name: draft.name,
      city: draft.city,
      country: draft.country,
      latitude,
      longitude,
    });
  };

  if (editing) {
    const input = (key: keyof Draft, label: string, placeholder: string) => (
      <label className="block space-y-1">
        <span className="text-xs text-muted-foreground">{label}</span>
        <Input
          value={draft[key]}
          onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}
          placeholder={placeholder}
          maxLength={
            key === "latitude" || key === "longitude"
              ? undefined
              : LOCATION_TEXT_MAX_LENGTH
          }
          inputMode={
            key === "latitude" || key === "longitude" ? "decimal" : undefined
          }
          disabled={saving}
        />
      </label>
    );
    return (
      <form
        onSubmit={submit}
        className="space-y-2 rounded-md border p-3"
        aria-label="Editar ubicación"
      >
        <p className="text-sm font-medium">Ubicación</p>
        {input("name", "Lugar", "Ej: Tienda de ejemplo")}
        <div className="grid grid-cols-2 gap-2">
          {input("city", "Ciudad", "Ej: Ciudad Ficticia")}
          {input("country", "País", "Ej: Chile")}
          {input("latitude", "Latitud", "-33.000000")}
          {input("longitude", "Longitud", "-70.000000")}
        </div>
        {error && (
          <p role="alert" className="text-sm text-red-600">
            {error}
          </p>
        )}
        <div className="flex flex-wrap gap-2 pt-1">
          <Button type="submit" size="sm" disabled={saving}>
            {saving ? "Guardando..." : "Guardar"}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={saving}
            onClick={() => setEditing(false)}
          >
            Cancelar
          </Button>
          {location && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="ml-auto text-red-600"
              disabled={saving}
              onClick={() => void save(null)}
            >
              Quitar ubicación
            </Button>
          )}
        </div>
      </form>
    );
  }

  if (!location) {
    return (
      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="button"
          variant="ghost"
          size="xs"
          className="text-muted-foreground"
          onClick={startEditing}
        >
          <MapPin />
          Agregar ubicación
        </Button>
        {isManual && (
          // Cleared by the user: scrapes will not fill it back in.
          <span className="text-xs text-muted-foreground">
            Ubicación quitada manualmente
          </span>
        )}
      </div>
    );
  }

  const text = formatLocationText(location);
  const coordinates = formatCoordinates(location);
  const mapUrl = openStreetMapUrl(location);
  return renderRow(
    "Ubicación",
    <span className="flex items-start gap-1">
      <span className="min-w-0 flex-1 space-y-0.5">
        {text && <span className="block">{text}</span>}
        {coordinates && mapUrl && (
          <a
            href={mapUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-xs text-muted-foreground underline-offset-4 hover:underline"
          >
            {coordinates}
            <ExternalLink className="size-3" aria-hidden />
            <span className="sr-only">(abrir en OpenStreetMap)</span>
          </a>
        )}
        {isManual && (
          <Badge variant="secondary" className="mt-0.5">
            manual
          </Badge>
        )}
      </span>
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        className="-mt-0.5 shrink-0"
        onClick={startEditing}
        aria-label="Editar ubicación"
        title="Editar ubicación"
      >
        <Pencil />
      </Button>
    </span>
  );
}
