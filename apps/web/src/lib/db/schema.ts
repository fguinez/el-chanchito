import {
  pgTable,
  uuid,
  text,
  integer,
  boolean,
  date,
  timestamp,
  numeric,
  jsonb,
  uniqueIndex,
  index,
  type AnyPgColumn,
} from "drizzle-orm/pg-core";
import { relations, sql } from "drizzle-orm";
import type {
  ProductAttributes,
  ProductKind,
  ProductMetrics,
} from "@chanchito/product-model";
import type { MonitorDisplay, MonitorThreshold } from "@/lib/monitors/types";

// Kind vocabulary, roles, and labels are generated from the shared registry
// (packages/product-model); re-exported here so callers keep one import path.
export {
  PRODUCT_KINDS,
  ASSET_KINDS,
  LIABILITY_KINDS,
  KIND_INFO,
} from "@chanchito/product-model";
export type {
  ProductAttributes,
  ProductKind,
  ProductMetrics,
} from "@chanchito/product-model";

// ---------------------------------------------------------------------------
// Core hierarchy: users -> accounts (enrollment at an institution) -> products
// ---------------------------------------------------------------------------

// Users (schema.org/Person)
export const users = pgTable("users", {
  id: uuid("id").primaryKey().defaultRandom(),
  name: text("name").notNull(),
  email: text("email").unique(),
  createdAt: timestamp("created_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
  updatedAt: timestamp("updated_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
});

// Institutions: the platform — bank, fintech, exchange (schema.org/FinancialService)
export const institutions = pgTable("institutions", {
  id: uuid("id").primaryKey().defaultRandom(),
  slug: text("slug").notNull().unique(),
  name: text("name").notNull(),
  kind: text("kind").notNull().default("other"),
  country: text("country"),
  url: text("url"),
  createdAt: timestamp("created_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
  updatedAt: timestamp("updated_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
});

// Accounts: a user's enrollment at one institution (what you log into)
export const accounts = pgTable(
  "accounts",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: uuid("user_id")
      .notNull()
      .references(() => users.id),
    institutionId: uuid("institution_id")
      .notNull()
      .references(() => institutions.id),
    name: text("name").notNull().default("Personal"),
    isActive: boolean("is_active").notNull().default(true),
    displayOrder: integer("display_order").notNull().default(0),
    createdAt: timestamp("created_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
    updatedAt: timestamp("updated_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
  },
  (table) => [
    uniqueIndex("uq_accounts_user_institution_name").on(
      table.userId,
      table.institutionId,
      table.name
    ),
    index("idx_accounts_user").on(table.userId),
    index("idx_accounts_institution").on(table.institutionId),
  ]
);

// Products: the money-holding elements (schema.org/FinancialProduct).
// Renamed from the old `accounts` table — rows kept their UUIDs.
// Per-kind payloads (typed by the shared registry):
//   attributes -> slow-changing identity/config, shallow-merged on write
//   metrics    -> latest per-scrape observation; history in product_snapshots
export const products = pgTable(
  "products",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    accountId: uuid("account_id")
      .notNull()
      .references(() => accounts.id),
    parentProductId: uuid("parent_product_id").references(
      (): AnyPgColumn => products.id
    ),
    kind: text("kind").$type<ProductKind>().notNull(),
    name: text("name").notNull(),
    // The scrapers' name for the product (V027), refreshed on every scrape;
    // `name` equals it until the user renames the product.
    defaultName: text("default_name").notNull(),
    // Create-only, institution-unique identifier; never changes on rename.
    slug: text("slug").notNull(),
    currency: text("currency").notNull().default("CLP"),
    externalRef: text("external_ref"),
    attributes: jsonb("attributes")
      .$type<ProductAttributes | Record<string, never>>()
      .notNull()
      .default({}),
    metrics: jsonb("metrics").$type<ProductMetrics | null>(),
    // Denormalized latest headline (metrics.headline()); history lives in
    // product_snapshots. balance_as_of also moves when an unchanged balance
    // is re-confirmed.
    currentBalance: numeric("current_balance", { precision: 20, scale: 8 }),
    balanceAsOf: timestamp("balance_as_of", { withTimezone: true }),
    isActive: boolean("is_active").notNull().default(true),
    displayOrder: integer("display_order").notNull().default(0),
    createdAt: timestamp("created_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
    updatedAt: timestamp("updated_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
  },
  (table) => [
    uniqueIndex("uq_products_account_slug").on(table.accountId, table.slug),
    index("idx_products_account").on(table.accountId),
  ]
);

// Product observation history: one row per metrics change (not one per scrape).
// `balance` is the headline; `metrics` the full typed payload at `as_of`
// (empty `{}` for rows that predate typed observations).
export const productSnapshots = pgTable(
  "product_snapshots",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    productId: uuid("product_id")
      .notNull()
      .references(() => products.id),
    balance: numeric("balance", { precision: 20, scale: 8 }).notNull(),
    metrics: jsonb("metrics")
      .$type<ProductMetrics | Record<string, never>>()
      .notNull()
      .default({}),
    asOf: timestamp("as_of", { withTimezone: true }).notNull().defaultNow(),
    source: text("source").notNull().default("scraper"),
  },
  (table) => [
    uniqueIndex("product_snapshots_product_id_as_of_key").on(
      table.productId,
      table.asOf
    ),
    index("idx_product_snapshots_product").on(table.productId),
  ]
);

// Exchange rates an institution publishes itself, read by its scraper (V024).
// product_id NULL = institution-wide; a product-scoped row also carries the
// product's institution_id. Current value only: a rescrape overwrites it and
// moves observed_at. Resolution and staleness live in lib/rates.
export const exchangeRates = pgTable(
  "exchange_rates",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    institutionId: uuid("institution_id")
      .notNull()
      .references(() => institutions.id, { onDelete: "cascade" }),
    productId: uuid("product_id").references(() => products.id, {
      onDelete: "cascade",
    }),
    baseCurrency: text("base_currency").notNull(),
    quoteCurrency: text("quote_currency").notNull().default("CLP"),
    // Quote units per 1 base unit.
    rate: numeric("rate", { precision: 20, scale: 8 }).notNull(),
    // Spanish UI label of where the rate was read.
    source: text("source").notNull(),
    // Validity date printed by the institution, when it shows one.
    reportedAsOf: date("reported_as_of"),
    // Last scrape that read this value.
    observedAt: timestamp("observed_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
    createdAt: timestamp("created_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
    updatedAt: timestamp("updated_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
  },
  (table) => [
    uniqueIndex("exchange_rates_scope_pair_idx").on(
      table.institutionId,
      sql`COALESCE(${table.productId}, '00000000-0000-0000-0000-000000000000'::uuid)`,
      table.baseCurrency,
      table.quoteCurrency
    ),
  ]
);

// ---------------------------------------------------------------------------
// Transactions & categorization
// ---------------------------------------------------------------------------

// Categories
export const categories = pgTable("categories", {
  id: uuid("id").primaryKey().defaultRandom(),
  name: text("name").notNull(),
  parentId: uuid("parent_id").references((): AnyPgColumn => categories.id),
  color: text("color"),
  icon: text("icon"),
  createdAt: timestamp("created_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
});

// Category auto-assignment rules
export const categoryRules = pgTable(
  "category_rules",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    keyword: text("keyword").notNull(),
    categoryId: uuid("category_id")
      .notNull()
      .references(() => categories.id, { onDelete: "cascade" }),
    priority: integer("priority").notNull().default(0),
    createdAt: timestamp("created_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
  },
  (table) => [index("idx_category_rules_priority").on(table.priority)]
);

// Transactions: one money movement on a product
export const transactions = pgTable(
  "transactions",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    productId: uuid("product_id")
      .notNull()
      .references(() => products.id),
    description: text("description").notNull(),
    // Signed, in the product's currency (products.currency). NUMERIC(18, 2)
    // since V023 so foreign-currency movements keep their cents. postgres.js
    // hands NUMERIC over as a string; mode "number" converts it on every
    // select of this column, so callers keep getting (and inserting) numbers.
    // Raw sql`` over the column (e.g. SUM(amount)) bypasses that mapping and
    // must convert its result itself.
    amount: numeric("amount", { precision: 18, scale: 2, mode: "number" })
      .notNull(),
    transactionDate: date("transaction_date").notNull(),
    // When the institution posted the movement, where it reports that apart
    // from when the movement occurred (V019). Nullable: most sources report a
    // single date. Never part of a dedup key.
    accountingDate: date("accounting_date"),
    categoryId: uuid("category_id").references(() => categories.id),
    scheduledMonth: date("scheduled_month"),
    source: text("source").notNull().default("manual"),
    externalId: text("external_id"),
    isInternalTransfer: boolean("is_internal_transfer")
      .notNull()
      .default(false),
    isManuallyCategorized: boolean("is_manually_categorized")
      .notNull()
      .default(false),
    notes: text("notes"),
    // Optional location (V025), every column independent; the coordinates are
    // set together or not at all. Scrapers only fill NULL columns, and never
    // touch a row whose location the user set or cleared (locationIsManual).
    locationName: text("location_name"),
    locationCity: text("location_city"),
    locationCountry: text("location_country"),
    locationLatitude: numeric("location_latitude", {
      precision: 9,
      scale: 6,
      mode: "number",
    }),
    locationLongitude: numeric("location_longitude", {
      precision: 9,
      scale: 6,
      mode: "number",
    }),
    locationIsManual: boolean("location_is_manual").notNull().default(false),
    createdAt: timestamp("created_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
    updatedAt: timestamp("updated_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
  },
  (table) => [
    uniqueIndex("uq_transactions_product_external").on(
      table.productId,
      table.externalId
    ),
    index("idx_transactions_product_date").on(
      table.productId,
      table.transactionDate
    ),
    index("idx_transactions_scheduled_month").on(table.scheduledMonth),
  ]
);

// ---------------------------------------------------------------------------
// Wealth & recurring items
// ---------------------------------------------------------------------------

// Wealth snapshots: legacy totals for pre-migration dates. The wealth series
// is derived from product_snapshots (see lib/wealth); these rows are
// read-only history, except for backdating through POST /api/wealth before
// the derived series starts.
export const wealthSnapshots = pgTable("wealth_snapshots", {
  id: uuid("id").primaryKey().defaultRandom(),
  snapshotDate: date("snapshot_date").notNull().unique(),
  patrimonio: integer("patrimonio").notNull(),
  deuda: integer("deuda").notNull().default(0),
  fintualBalance: integer("fintual_balance"),
  mercadopagoBalance: integer("mercadopago_balance"),
  banchileSavings: integer("banchile_savings"),
  notes: text("notes"),
  createdAt: timestamp("created_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
  updatedAt: timestamp("updated_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
});

// Fixed expenses
export const fixedExpenses = pgTable("fixed_expenses", {
  id: uuid("id").primaryKey().defaultRandom(),
  name: text("name").notNull(),
  amount: integer("amount").notNull(),
  isShared: boolean("is_shared").notNull().default(false),
  sharedRatio: numeric("shared_ratio", { precision: 5, scale: 4 }),
  activeFrom: date("active_from"),
  activeTo: date("active_to"),
  createdAt: timestamp("created_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
  updatedAt: timestamp("updated_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
});

// Internal transfer lifecycle. The column has no CHECK constraint (V004), so
// the API validates against this list.
export const TRANSFER_STATUSES = ["pending", "resolved"] as const;
export type TransferStatus = (typeof TRANSFER_STATUSES)[number];

// Internal transfers (between products)
export const internalTransfers = pgTable("internal_transfers", {
  id: uuid("id").primaryKey().defaultRandom(),
  description: text("description").notNull(),
  amount: integer("amount").notNull(),
  fromProductId: uuid("from_product_id").references(() => products.id),
  toProductId: uuid("to_product_id").references(() => products.id),
  // The scraped rows this transfer accounts for (V028): the outgoing one on
  // fromProductId and the incoming one on toProductId. Each is optional (a
  // product without a transaction feed has nothing to link) and unique, so a
  // row serves at most one transfer. While linked, the row carries
  // is_internal_transfer and drops out of the Gastos/Ingresos totals.
  fromTransactionId: uuid("from_transaction_id")
    .references(() => transactions.id, { onDelete: "set null" })
    .unique("uq_internal_transfers_from_transaction"),
  toTransactionId: uuid("to_transaction_id")
    .references(() => transactions.id, { onDelete: "set null" })
    .unique("uq_internal_transfers_to_transaction"),
  transferDate: date("transfer_date").notNull(),
  status: text("status").$type<TransferStatus>().notNull().default("pending"),
  notes: text("notes"),
  createdAt: timestamp("created_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
  updatedAt: timestamp("updated_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
});

// Scraper runs
export const scraperRuns = pgTable(
  "scraper_runs",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    method: text("method").notNull(),
    institution: text("institution").notNull(),
    startedAt: timestamp("started_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
    finishedAt: timestamp("finished_at", { withTimezone: true }),
    status: text("status").notNull().default("running"),
    transactionsImported: integer("transactions_imported").default(0),
    errorMessage: text("error_message"),
  },
  (table) => [
    index("idx_scraper_runs_method_institution_date").on(
      table.method,
      table.institution,
      table.startedAt,
    ),
  ]
);

// ---------------------------------------------------------------------------
// Monitors
// ---------------------------------------------------------------------------

// User-defined monitors: one left expression plus {severity, comparator,
// expression} thresholds, all persisted in uuid-ref form and evaluated on
// read (see lib/monitors). display is passed raw like product metrics.
export const monitors = pgTable("monitors", {
  id: uuid("id").primaryKey().defaultRandom(),
  name: text("name").notNull(),
  description: text("description"),
  currency: text("currency").notNull().default("CLP"),
  expression: text("expression").notNull(),
  thresholds: jsonb("thresholds").$type<MonitorThreshold[]>().notNull(),
  display: jsonb("display")
    .$type<MonitorDisplay>()
    .notNull()
    .default({ chart: "line", show_margin: true }),
  isActive: boolean("is_active").notNull().default(true),
  createdAt: timestamp("created_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
  updatedAt: timestamp("updated_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
});

// Monitor adjustments ("variaciones"): from adjustment_date to the end of
// that month, amount (monitor currency) is added to every threshold of the
// monitor. Applied on read (see lib/monitors/evaluate adjustmentOnDate).
export const monitorAdjustments = pgTable(
  "monitor_adjustments",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    monitorId: uuid("monitor_id")
      .notNull()
      .references(() => monitors.id, { onDelete: "cascade" }),
    adjustmentDate: date("adjustment_date").notNull(),
    amount: numeric("amount", { precision: 20, scale: 8 }).notNull(),
    description: text("description"),
    createdAt: timestamp("created_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
    updatedAt: timestamp("updated_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
  },
  (table) => [
    index("idx_monitor_adjustments_monitor").on(
      table.monitorId,
      table.adjustmentDate
    ),
  ]
);

// ---------------------------------------------------------------------------
// Relations
// ---------------------------------------------------------------------------

export const usersRelations = relations(users, ({ many }) => ({
  accounts: many(accounts),
}));

export const institutionsRelations = relations(institutions, ({ many }) => ({
  accounts: many(accounts),
}));

export const accountsRelations = relations(accounts, ({ one, many }) => ({
  user: one(users, {
    fields: [accounts.userId],
    references: [users.id],
  }),
  institution: one(institutions, {
    fields: [accounts.institutionId],
    references: [institutions.id],
  }),
  products: many(products),
}));

export const productsRelations = relations(products, ({ one, many }) => ({
  account: one(accounts, {
    fields: [products.accountId],
    references: [accounts.id],
  }),
  parent: one(products, {
    fields: [products.parentProductId],
    references: [products.id],
  }),
  transactions: many(transactions),
  snapshots: many(productSnapshots),
}));

export const productSnapshotsRelations = relations(
  productSnapshots,
  ({ one }) => ({
    product: one(products, {
      fields: [productSnapshots.productId],
      references: [products.id],
    }),
  })
);

export const transactionsRelations = relations(transactions, ({ one }) => ({
  product: one(products, {
    fields: [transactions.productId],
    references: [products.id],
  }),
  category: one(categories, {
    fields: [transactions.categoryId],
    references: [categories.id],
  }),
}));

