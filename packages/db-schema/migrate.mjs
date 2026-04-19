import postgres from "postgres";
import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";

const sql = postgres(
  process.env.DATABASE_URL || "postgres://finance:finance@localhost:5432/finance"
);

async function migrate() {
  // Create migrations tracking table
  await sql`
    CREATE TABLE IF NOT EXISTS _migrations (
      id SERIAL PRIMARY KEY,
      name TEXT NOT NULL UNIQUE,
      applied_at TIMESTAMPTZ DEFAULT now()
    )
  `;

  const migrationsDir = join(import.meta.dirname, "migrations");
  const files = (await readdir(migrationsDir))
    .filter((f) => f.endsWith(".sql"))
    .sort();

  const applied = await sql`SELECT name FROM _migrations`;
  const appliedSet = new Set(applied.map((r) => r.name));

  for (const file of files) {
    if (appliedSet.has(file)) {
      console.log(`  skip: ${file}`);
      continue;
    }

    const content = await readFile(join(migrationsDir, file), "utf-8");
    console.log(`  apply: ${file}`);

    await sql.begin(async (tx) => {
      await tx.unsafe(content);
      await tx`INSERT INTO _migrations (name) VALUES (${file})`;
    });
  }

  console.log("Migrations complete.");
  await sql.end();
}

migrate().catch((err) => {
  console.error("Migration failed:", err);
  process.exit(1);
});
