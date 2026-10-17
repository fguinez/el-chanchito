import { describe, expect, it } from "vitest";
import {
  formatCoordinates,
  formatLocationText,
  locationFromColumns,
  openStreetMapUrl,
  validateLocationInput,
} from "@/lib/transaction-location";

// Synthetic places only: no real city, country or coordinates.
const EMPTY = {
  name: null,
  city: null,
  country: null,
  latitude: null,
  longitude: null,
};

describe("formatLocationText", () => {
  it.each([
    [{ ...EMPTY, name: "Tienda Ficticia", city: "Ciudad Ficticia", country: "XX" }, "Tienda Ficticia · Ciudad Ficticia, XX"],
    [{ ...EMPTY, city: "Ciudad Ficticia", country: "XX" }, "Ciudad Ficticia, XX"],
    [{ ...EMPTY, name: "Tienda Ficticia", country: "XX" }, "Tienda Ficticia · XX"],
    [{ ...EMPTY, name: "Tienda Ficticia" }, "Tienda Ficticia"],
    [{ ...EMPTY, country: "XX" }, "XX"],
  ])("composes %o", (location, text) => {
    expect(formatLocationText(location)).toBe(text);
  });

  it("is null without text, even with coordinates", () => {
    expect(formatLocationText(null)).toBeNull();
    const coordinatesOnly = { ...EMPTY, latitude: -33, longitude: -70 };
    expect(formatLocationText(coordinatesOnly)).toBeNull();
  });
});

describe("coordinates", () => {
  const located = { ...EMPTY, latitude: -33, longitude: -70.5 };

  it("formats both with six decimals", () => {
    expect(formatCoordinates(located)).toBe("-33.000000, -70.500000");
  });

  it("links to OpenStreetMap centred on them", () => {
    expect(openStreetMapUrl(located)).toBe(
      "https://www.openstreetmap.org/?mlat=-33.000000&mlon=-70.500000#map=16/-33.000000/-70.500000"
    );
  });

  it("is null without coordinates", () => {
    expect(formatCoordinates(EMPTY)).toBeNull();
    expect(openStreetMapUrl(null)).toBeNull();
  });

  it("keeps zero as a real coordinate", () => {
    expect(formatCoordinates({ ...EMPTY, latitude: 0, longitude: 0 })).toBe(
      "0.000000, 0.000000"
    );
  });
});

describe("locationFromColumns", () => {
  it("is null when every column is null", () => {
    expect(
      locationFromColumns({
        locationName: null,
        locationCity: null,
        locationCountry: null,
        locationLatitude: null,
        locationLongitude: null,
      })
    ).toBeNull();
  });
});

describe("validateLocationInput", () => {
  it("accepts null and an empty object as no location", () => {
    expect(validateLocationInput(null)).toEqual({ ok: true, value: null });
    expect(validateLocationInput({})).toEqual({ ok: true, value: null });
  });

  it("trims text and rounds coordinates to six decimals", () => {
    expect(
      validateLocationInput({
        city: " Ciudad Ficticia ",
        latitude: -33.1234567,
        longitude: 0,
      })
    ).toEqual({
      ok: true,
      value: { ...EMPTY, city: "Ciudad Ficticia", latitude: -33.123457, longitude: 0 },
    });
  });

  it("accepts the range boundaries", () => {
    expect(validateLocationInput({ latitude: -90, longitude: 180 }).ok).toBe(true);
  });

  it.each([
    [[], "location"],
    [{ latitude: Number.NaN, longitude: 0 }, "location.latitude"],
    [{ latitude: 0, longitude: Number.POSITIVE_INFINITY }, "location.longitude"],
    [{ country: "x".repeat(201) }, "location.country"],
  ])("rejects %o", (raw, field) => {
    const result = validateLocationInput(raw);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.field).toBe(field);
  });
});
