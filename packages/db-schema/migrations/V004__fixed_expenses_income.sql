-- Fixed monthly expenses (maps to Excel "Mensual fijo" sheet)
CREATE TABLE fixed_expenses (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  amount INTEGER NOT NULL,                        -- full amount (CLP)
  is_shared BOOLEAN NOT NULL DEFAULT false,
  shared_ratio NUMERIC(5,4),                      -- e.g. 0.69
  active_from DATE,
  active_to DATE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Income sources (maps to Excel "Porcentaje pagos" sheet)
CREATE TABLE income_sources (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  monthly_amount INTEGER NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Internal transfers (maps to Excel "Movimientos internos" sheet)
CREATE TABLE internal_transfers (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  description TEXT NOT NULL,
  amount INTEGER NOT NULL,
  from_account_id UUID REFERENCES accounts(id),
  to_account_id UUID REFERENCES accounts(id),
  transfer_date DATE NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',          -- 'pending' | 'resolved'
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
