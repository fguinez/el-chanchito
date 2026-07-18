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
import { relations } from "drizzle-orm";
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
    amount: integer("amount").notNull(),
    transactionDate: date("transaction_date").notNull(),
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
// Budget
// ---------------------------------------------------------------------------

// Budget configs (one per month)
export const budgetConfigs = pgTable("budget_configs", {
  id: uuid("id").primaryKey().defaultRandom(),
  month: date("month").notNull().unique(),
  variableBudget: integer("variable_budget").notNull(),
  fixedBudget: integer("fixed_budget").notNull(),
  creditCardLimit: integer("credit_card_limit").notNull(),
  checkingInitialBalance: integer("checking_initial_balance")
    .notNull()
    .default(0),
  salary: integer("salary").notNull(),
  sharedExpensesRatio: numeric("shared_expenses_ratio", {
    precision: 5,
    scale: 4,
  })
    .notNull()
    .default("0.6900"),
  dayStart: integer("day_start").notNull().default(1),
  createdAt: timestamp("created_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
  updatedAt: timestamp("updated_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
});

// Budget adjustments (variations)
export const budgetAdjustments = pgTable(
  "budget_adjustments",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    budgetConfigId: uuid("budget_config_id")
      .notNull()
      .references(() => budgetConfigs.id, { onDelete: "cascade" }),
    adjustmentDate: date("adjustment_date").notNull(),
    amount: integer("amount").notNull(),
    description: text("description"),
    createdAt: timestamp("created_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
  },
  (table) => [
    index("idx_budget_adjustments_config").on(
      table.budgetConfigId,
      table.adjustmentDate
    ),
  ]
);

// ---------------------------------------------------------------------------
// Wealth & recurring items
// ---------------------------------------------------------------------------

// Wealth snapshots: legacy totals for pre-migration dates + manual entries.
// The wealth series is now derived from product_snapshots (see /api/wealth).
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

// Income sources
export const incomeSources = pgTable("income_sources", {
  id: uuid("id").primaryKey().defaultRandom(),
  name: text("name").notNull(),
  monthlyAmount: integer("monthly_amount").notNull(),
  createdAt: timestamp("created_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
  updatedAt: timestamp("updated_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
});

// Internal transfers (between products)
export const internalTransfers = pgTable("internal_transfers", {
  id: uuid("id").primaryKey().defaultRandom(),
  description: text("description").notNull(),
  amount: integer("amount").notNull(),
  fromProductId: uuid("from_product_id").references(() => products.id),
  toProductId: uuid("to_product_id").references(() => products.id),
  transferDate: date("transfer_date").notNull(),
  status: text("status").notNull().default("pending"),
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

export const budgetConfigsRelations = relations(budgetConfigs, ({ many }) => ({
  adjustments: many(budgetAdjustments),
}));

export const budgetAdjustmentsRelations = relations(
  budgetAdjustments,
  ({ one }) => ({
    budgetConfig: one(budgetConfigs, {
      fields: [budgetAdjustments.budgetConfigId],
      references: [budgetConfigs.id],
    }),
  })
);
