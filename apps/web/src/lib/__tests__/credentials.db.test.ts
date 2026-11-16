import { execFileSync } from "node:child_process";
import { createHash, randomBytes, randomUUID } from "node:crypto";
import path from "node:path";
import postgres from "postgres";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { CREDENTIAL_ALGORITHM_V2, CREDENTIAL_SCHEME_V2 } from "@/lib/credentials/seal";
import { XWING_ENC_BYTES, XWING_PUBLIC_KEY_BYTES } from "@/lib/credentials/xwing";

// The dashboard's writes to the sealed credentials (V048-V051) against a
// real PostgreSQL: a save lands only under the active key, and one that waits
// on a rotation never lands under the key it retired. Runs only with
// TEST_DATABASE_URL naming a disposable server (`make test-db-up` starts one);
// without it the suite is skipped. Keys are random bytes and every sealed
// blob is filler: nothing here is a real credential.

const ADMIN_URL = process.env.TEST_DATABASE_URL;
const MIGRATE = path.resolve(__dirname, "../../../../../packages/db-schema/migrate.mjs");

type Lib = typeof import("@/lib/credentials/db");

// V050 ties the public key length to the algorithm, and V051 lets only v2
// keys become active and sets v2's sealed floor (1120-byte enc + 16-byte tag).
function fakeKey() {
  const publicKey = randomBytes(XWING_PUBLIC_KEY_BYTES);
  return { publicKey, keyId: createHash("sha256").update(publicKey).digest("hex") };
}

const SEALED = { fieldKey: "api_key", sealed: new Uint8Array(XWING_ENC_BYTES + 16).fill(7) };

describe.skipIf(!ADMIN_URL)("sealed credential writes (real PostgreSQL)", () => {
  const dbName = `chanchito_test_${randomUUID().replace(/-/g, "")}`;
  let admin: ReturnType<typeof postgres>;
  let sql: ReturnType<typeof postgres>;
  let lib: Lib;

  async function addKey(status: "active" | "retired") {
    const key = fakeKey();
    await sql`
      INSERT INTO credential_keys (key_id, algorithm, public_key, status, retired_at)
      VALUES (${key.keyId}, ${CREDENTIAL_ALGORITHM_V2}, ${key.publicKey}, ${status},
        ${status === "retired" ? new Date() : null})`;
    return key;
  }

  beforeAll(async () => {
    admin = postgres(ADMIN_URL as string, { max: 1 });
    await admin.unsafe(`CREATE DATABASE ${dbName}`);
    const url = new URL(ADMIN_URL as string);
    url.pathname = `/${dbName}`;
    execFileSync(process.execPath, [MIGRATE], {
      env: { ...process.env, DATABASE_URL: url.toString() },
      stdio: "ignore",
    });
    process.env.DATABASE_URL = url.toString();
    sql = postgres(url.toString(), { max: 2, onnotice: () => {} });
    lib = await import("@/lib/credentials/db");
  });

  beforeEach(async () => {
    await sql`DELETE FROM institution_credentials`;
    await sql`DELETE FROM credential_keys`;
  });

  afterAll(async () => {
    await sql?.end();
    if (admin) {
      await admin.unsafe(`DROP DATABASE IF EXISTS ${dbName} WITH (FORCE)`);
      await admin.end();
    }
  });

  it("stores under the active key and refuses a retired one", async () => {
    const retired = await addKey("retired");
    const active = await addKey("active");
    expect(await lib.upsertSealedFields("skipo", CREDENTIAL_SCHEME_V2, retired.keyId, [SEALED])).toBe(false);
    expect(await sql`SELECT 1 FROM institution_credentials`).toHaveLength(0);
    expect(await lib.upsertSealedFields("skipo", CREDENTIAL_SCHEME_V2, active.keyId, [SEALED])).toBe(true);
    const rows = await sql`SELECT key_id FROM institution_credentials`;
    expect(rows.map((r) => r.key_id)).toEqual([active.keyId]);
    expect((await lib.readSealingKey())?.keyId).toBe(active.keyId);
  });

  it("a save that waits on a rotation stores nothing under the retired key", async () => {
    const old = await addKey("active");
    const rotation = postgres(process.env.DATABASE_URL as string, { max: 1, onnotice: () => {} });
    try {
      let saving: Promise<boolean> | null = null;
      await rotation.begin(async (tx) => {
        await tx`SELECT 1 FROM credential_keys WHERE key_id = ${old.keyId} FOR UPDATE`;
        saving = lib.upsertSealedFields("skipo", CREDENTIAL_SCHEME_V2, old.keyId, [SEALED]);
        // Let the save reach its FOR SHARE and wait there.
        for (let i = 0; i < 100; i++) {
          const [waiting] = await sql`
            SELECT count(*)::int AS n FROM pg_stat_activity
            WHERE datname = current_database() AND wait_event_type = 'Lock'`;
          if (waiting.n > 0) break;
          await new Promise((resolve) => setTimeout(resolve, 20));
        }
        await tx`UPDATE credential_keys SET status = 'retired', retired_at = now()
          WHERE key_id = ${old.keyId}`;
      });
      expect(await saving).toBe(false);
    } finally {
      await rotation.end();
    }
    expect(await sql`SELECT 1 FROM institution_credentials`).toHaveLength(0);
  });

  it("reads the active key's dates and the last retirement", async () => {
    expect(await lib.readKeyInfo()).toBeNull();
    await addKey("retired");
    const active = await addKey("active");
    await sql`
      UPDATE credential_keys SET rotation_failed_at = now(), rotation_error = 'OperationalError'
      WHERE key_id = ${active.keyId}`;
    const info = await lib.readKeyInfo();
    expect(info?.createdAt).toBeInstanceOf(Date);
    expect(info?.lastRotatedAt).toBeInstanceOf(Date);
    expect(info?.rotationError).toBe("OperationalError");
  });
});
