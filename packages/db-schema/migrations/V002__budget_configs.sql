-- Budget configuration per month
CREATE TABLE budget_configs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  month DATE NOT NULL UNIQUE,                     -- first day of month
  variable_budget INTEGER NOT NULL,               -- presup_mensual_var (CLP)
  fixed_budget INTEGER NOT NULL,                  -- presup_mensual_fijo (CLP)
  credit_card_limit INTEGER NOT NULL,             -- cupo_tc (CLP)
  checking_initial_balance INTEGER NOT NULL DEFAULT 0,  -- monto_cc_inicial
  salary INTEGER NOT NULL,                        -- sueldo
  shared_expenses_ratio NUMERIC(5,4) NOT NULL DEFAULT 0.6900,
  day_start INTEGER NOT NULL DEFAULT 1,           -- dia_inicio
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Budget adjustments (maps to "Variaciones" column E in Planificacion)
CREATE TABLE budget_adjustments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  budget_config_id UUID NOT NULL REFERENCES budget_configs(id) ON DELETE CASCADE,
  adjustment_date DATE NOT NULL,
  amount INTEGER NOT NULL,                        -- positive=extra income, negative=extra expense
  description TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_budget_adjustments_config ON budget_adjustments(budget_config_id, adjustment_date);
