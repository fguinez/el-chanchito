import { describe, expect, it } from "vitest";

import { SAVE_BLOCKED_MESSAGES, saveBlocker } from "@/lib/credentials/availability";
import type { ControlCredentialStatus } from "@/lib/credentials/control";
import { describeKeyInfo, type CredentialKeyInfo } from "@/lib/credentials/key-info";
import type { SealingKey } from "@/lib/credentials/seal";

// Synthetic key ids and dates only.
const KEY: SealingKey = {
  keyId: "a".repeat(64),
  algorithm: "x",
  publicKey: new Uint8Array(32),
};

function control(extra: Partial<ControlCredentialStatus> = {}): ControlCredentialStatus {
  return { key_available: true, key_id: KEY.keyId, fields: {}, ...extra };
}

describe("saveBlocker", () => {
  it.each([
    ["service_unreachable", KEY, null],
    ["key_unavailable", KEY, control({ key_available: false, key_id: null })],
    ["key_mismatch", KEY, control({ key_id: "b".repeat(64), key_mismatch: true })],
    ["no_key", null, control()],
    ["key_pending", KEY, control({ key_id: "b".repeat(64) })],
    ["pending_unresolved", KEY, control({ state: "pending_unresolved" })],
  ] as const)("is %s", (reason, key, status) => {
    expect(saveBlocker(key, status)).toBe(reason);
    expect(SAVE_BLOCKED_MESSAGES[reason]).toBeTruthy();
  });

  it("lets a save through when the service holds the active key", () => {
    expect(saveBlocker(KEY, control())).toBeNull();
  });
});

describe("describeKeyInfo", () => {
  const now = new Date("2026-03-02T12:00:00Z");
  const base: CredentialKeyInfo = {
    createdAt: "2026-01-01T12:00:00Z",
    lastRotatedAt: null,
    rotationFailedAt: null,
    rotationError: null,
    maxAgeDays: 90,
  };

  it("gives the key's age and the next rotation", () => {
    const lines = describeKeyInfo(base, now);
    expect(lines.age).toContain("hace 60 días");
    expect(lines.age).toContain("Aún no se ha rotado");
    expect(lines.schedule).toContain("cada 90 días");
    expect(lines.schedule).toContain("cerca del");
    expect(lines.failure).toBeNull();
  });

  it("says when rotation is off, overdue or unknown", () => {
    expect(describeKeyInfo({ ...base, maxAgeDays: 0 }, now).schedule).toContain("desactivada");
    expect(describeKeyInfo({ ...base, maxAgeDays: 30 }, now).schedule).toContain("pendiente");
    expect(describeKeyInfo({ ...base, maxAgeDays: null }, now).schedule).toBeNull();
  });

  it("shows the last rotation and a failure with its reason", () => {
    const lines = describeKeyInfo(
      {
        ...base,
        lastRotatedAt: "2026-01-01T12:00:00Z",
        rotationFailedAt: "2026-03-01T12:00:00Z",
        rotationError: "OperationalError",
      },
      now
    );
    expect(lines.age).toContain("Última rotación");
    expect(lines.failure).toContain("falló");
    expect(lines.failure).toContain("OperationalError");
  });
});
