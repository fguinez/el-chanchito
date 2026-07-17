-- Wealth snapshots
CREATE TABLE wealth_snapshots (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  snapshot_date DATE NOT NULL UNIQUE,
  patrimonio INTEGER NOT NULL,                   -- total assets
  deuda INTEGER NOT NULL DEFAULT 0,              -- total debt
  fintual_balance INTEGER,
  mercadopago_balance INTEGER,
  banchile_savings INTEGER,
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
