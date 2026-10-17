// A transaction's optional location (V025): request validation, display
// formatting and the map link. Pure (no db, no next imports) so the routes,
// the detail panel and the tests share one definition.

import type {
  ValidationFailure,
  ValidationResult,
} from "@/lib/monitors/validate";

/** Every field optional and independent; coordinates come in pairs. */
export interface TransactionLocation {
  name: string | null;
  city: string | null;
  country: string | null;
  latitude: number | null;
  longitude: number | null;
}

/** Same bound as product_model.LOCATION_TEXT_MAX_LENGTH. */
export const LOCATION_TEXT_MAX_LENGTH = 200;
/** location_latitude / location_longitude are NUMERIC(9, 6). */
const COORDINATE_DECIMALS = 6;

const TEXT_FIELDS = ["name", "city", "country"] as const;
const COORDINATE_FIELDS = ["latitude", "longitude"] as const;
const FIELDS: readonly string[] = [...TEXT_FIELDS, ...COORDINATE_FIELDS];
const RANGES = { latitude: 90, longitude: 180 } as const;

/** The columns as the db row carries them (NUMERIC arrives as a number). */
export interface LocationColumns {
  locationName: string | null;
  locationCity: string | null;
  locationCountry: string | null;
  locationLatitude: number | null;
  locationLongitude: number | null;
}

function fail(error: string, field: string): ValidationFailure {
  return { ok: false, status: 400, error, field };
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** True when no field carries a value. */
export function isEmptyLocation(location: TransactionLocation): boolean {
  return (
    location.name === null &&
    location.city === null &&
    location.country === null &&
    location.latitude === null &&
    location.longitude === null
  );
}

/** The row's columns as a location, or null when none of them is set. */
export function locationFromColumns(
  row: LocationColumns
): TransactionLocation | null {
  const location: TransactionLocation = {
    name: row.locationName,
    city: row.locationCity,
    country: row.locationCountry,
    latitude: row.locationLatitude,
    longitude: row.locationLongitude,
  };
  return isEmptyLocation(location) ? null : location;
}

/** A location as the columns an insert or update writes. */
export function locationToColumns(
  location: TransactionLocation | null
): LocationColumns {
  return {
    locationName: location?.name ?? null,
    locationCity: location?.city ?? null,
    locationCountry: location?.country ?? null,
    locationLatitude: location?.latitude ?? null,
    locationLongitude: location?.longitude ?? null,
  };
}

/**
 * Validate and normalize a location from a request body. `null` (or an object
 * whose fields are all blank) means "no location". Text is trimmed, blank
 * becomes null, and anything longer than LOCATION_TEXT_MAX_LENGTH is a 400;
 * coordinates must be finite numbers in range, given together, and are
 * rounded to the stored 6 decimals. Unknown keys are rejected so a typo never
 * silently drops a value. Failures name the field as `location.<key>`.
 */
export function validateLocationInput(
  raw: unknown
): ValidationResult<TransactionLocation | null> {
  if (raw === null) return { ok: true, value: null };
  if (!isPlainObject(raw)) {
    return fail("Field 'location' must be an object or null", "location");
  }
  for (const key of Object.keys(raw)) {
    if (!FIELDS.includes(key)) {
      return fail(`Unknown location field '${key}'`, `location.${key}`);
    }
  }

  const location: TransactionLocation = {
    name: null,
    city: null,
    country: null,
    latitude: null,
    longitude: null,
  };
  for (const key of TEXT_FIELDS) {
    const value = raw[key];
    if (value === undefined || value === null) continue;
    if (typeof value !== "string") {
      return fail(
        `Field 'location.${key}' must be a string or null`,
        `location.${key}`
      );
    }
    const text = value.trim();
    if (text.length > LOCATION_TEXT_MAX_LENGTH) {
      return fail(
        `Field 'location.${key}' must be at most ${LOCATION_TEXT_MAX_LENGTH} characters`,
        `location.${key}`
      );
    }
    location[key] = text === "" ? null : text;
  }
  for (const key of COORDINATE_FIELDS) {
    const value = raw[key];
    if (value === undefined || value === null) continue;
    if (typeof value !== "number" || !Number.isFinite(value)) {
      return fail(
        `Field 'location.${key}' must be a number or null`,
        `location.${key}`
      );
    }
    if (Math.abs(value) > RANGES[key]) {
      return fail(
        `Field 'location.${key}' must be between -${RANGES[key]} and ${RANGES[key]}`,
        `location.${key}`
      );
    }
    location[key] = Number(value.toFixed(COORDINATE_DECIMALS));
  }
  if ((location.latitude === null) !== (location.longitude === null)) {
    const missing = location.latitude === null ? "latitude" : "longitude";
    return fail(
      "Fields 'location.latitude' and 'location.longitude' must be given together",
      `location.${missing}`
    );
  }
  return { ok: true, value: isEmptyLocation(location) ? null : location };
}

/** "Lugar · Ciudad, País", skipping whatever is missing; null when no text. */
export function formatLocationText(
  location: Pick<TransactionLocation, "name" | "city" | "country"> | null
): string | null {
  if (!location) return null;
  const place = [location.city, location.country]
    .filter((part): part is string => !!part)
    .join(", ");
  const text = [location.name, place]
    .filter((part): part is string => !!part)
    .join(" · ");
  return text === "" ? null : text;
}

/** "-33.000000, -70.000000"; null without coordinates. */
export function formatCoordinates(
  location: Pick<TransactionLocation, "latitude" | "longitude"> | null
): string | null {
  if (location?.latitude == null || location.longitude == null) return null;
  return `${location.latitude.toFixed(COORDINATE_DECIMALS)}, ${location.longitude.toFixed(COORDINATE_DECIMALS)}`;
}

/** An OpenStreetMap link centred on the coordinates; null without them. */
export function openStreetMapUrl(
  location: Pick<TransactionLocation, "latitude" | "longitude"> | null
): string | null {
  if (location?.latitude == null || location.longitude == null) return null;
  const lat = location.latitude.toFixed(COORDINATE_DECIMALS);
  const lng = location.longitude.toFixed(COORDINATE_DECIMALS);
  return `https://www.openstreetmap.org/?mlat=${lat}&mlon=${lng}#map=16/${lat}/${lng}`;
}
