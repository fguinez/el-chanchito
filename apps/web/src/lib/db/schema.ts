import {
  pgTable,
  uuid,
  text,
  integer,
  boolean,
  date,
  timestamp,
  numeric,
  serial,
  uniqueIndex,
  index,
} from "drizzle-orm/pg-core";
import { relations } from "drizzle-orm";

// Accounts
export const accounts = pgTable("accounts", {
  id: uuid("id").primaryKey().defaultRandom(),
  name: text("name").notNull(),
  institution: text("institution").notNull(),
  accountType: text("account_type").notNull(),
  currency: text("currency").notNull().default("CLP"),
  isActive: boolean("is_active").notNull().default(true),
  displayOrder: integer("display_order").notNull().default(0),
  createdAt: timestamp("created_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
  updatedAt: timestamp("updated_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
});

// Categories
export const categories = pgTable("categories", {
  id: uuid("id").primaryKey().defaultRandom(),
  name: text("name").notNull(),
  parentId: uuid("parent_id").references((): any => categories.id),
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

// Transactions
export const transactions = pgTable(
  "transactions",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    accountId: uuid("account_id")
      .notNull()
      .references(() => accounts.id),
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
    uniqueIndex("uq_transactions_account_external").on(
      table.accountId,
      table.externalId
    ),
    index("idx_transactions_account_date").on(
      table.accountId,
      table.transactionDate
    ),
    index("idx_transactions_scheduled_month").on(table.scheduledMonth),
  ]
);

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

// Wealth snapshots
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

// Internal transfers
export const internalTransfers = pgTable("internal_transfers", {
  id: uuid("id").primaryKey().defaultRandom(),
  description: text("description").notNull(),
  amount: integer("amount").notNull(),
  fromAccountId: uuid("from_account_id").references(() => accounts.id),
  toAccountId: uuid("to_account_id").references(() => accounts.id),
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
    scraperName: text("scraper_name").notNull(),
    startedAt: timestamp("started_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
    finishedAt: timestamp("finished_at", { withTimezone: true }),
    status: text("status").notNull().default("running"),
    transactionsImported: integer("transactions_imported").default(0),
    errorMessage: text("error_message"),
  },
  (table) => [
    index("idx_scraper_runs_name_date").on(table.scraperName, table.startedAt),
  ]
);

// Account balances (latest per account, updated by scrapers)
export const accountBalances = pgTable(
  "account_balances",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    accountId: uuid("account_id")
      .notNull()
      .references(() => accounts.id)
      .unique(),
    balance: integer("balance").notNull(),
    asOf: timestamp("as_of", { withTimezone: true }).notNull().defaultNow(),
    source: text("source").notNull().default("scraper"),
  },
  (table) => [
    index("idx_account_balances_account").on(table.accountId),
  ]
);

// Relations
export const accountsRelations = relations(accounts, ({ many, one }) => ({
  transactions: many(transactions),
  balance: one(accountBalances, {
    fields: [accounts.id],
    references: [accountBalances.accountId],
  }),
}));

export const transactionsRelations = relations(transactions, ({ one }) => ({
  account: one(accounts, {
    fields: [transactions.accountId],
    references: [accounts.id],
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
