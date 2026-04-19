-- Category auto-assignment rules (keyword -> category)
CREATE TABLE category_rules (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  keyword TEXT NOT NULL,              -- substring to match in transaction description (case-insensitive)
  category_id UUID NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
  priority INTEGER NOT NULL DEFAULT 0, -- higher = checked first
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_category_rules_priority ON category_rules(priority DESC);

-- Seed default categories
INSERT INTO categories (id, name, color, icon) VALUES
  (gen_random_uuid(), 'Supermercado', '#22c55e', 'shopping-cart'),
  (gen_random_uuid(), 'Transporte', '#3b82f6', 'car'),
  (gen_random_uuid(), 'Restaurantes', '#f97316', 'utensils'),
  (gen_random_uuid(), 'Entretenimiento', '#a855f7', 'gamepad-2'),
  (gen_random_uuid(), 'Salud', '#ef4444', 'heart-pulse'),
  (gen_random_uuid(), 'Servicios', '#6b7280', 'settings'),
  (gen_random_uuid(), 'Transferencias', '#64748b', 'arrow-left-right'),
  (gen_random_uuid(), 'Otros', '#94a3b8', 'circle');
