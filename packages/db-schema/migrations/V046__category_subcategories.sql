-- V046: Subcategories, one level deep.
--
-- `categories.parent_id` exists since V001 but nothing used it. It now makes a
-- category a subcategory of a top-level one (for example one per trip under
-- "Viajes"). A transaction still has a single `category_id`, which may point
-- at a top-level category or at a subcategory; a subcategory's rows also count
-- toward its parent wherever spend is shown per top-level category (the
-- dashboard rolls them up; nothing is copied here).
--
-- The tree stays one level deep, enforced by a trigger because a CHECK cannot
-- look at other rows:
--
--   * a category cannot be its own parent;
--   * a parent must itself be top-level (no grandchildren);
--   * a category that has subcategories cannot become one.
--
-- The trigger share-locks the parent row, so a concurrent write that would
-- give the parent a parent of its own waits for this one (and then fails its
-- own check), instead of both racing past the checks. Deleting a parent is
-- handled by the API, which promotes its subcategories to top-level first.
--
-- No backfill: every existing category is top-level. Idempotent.

CREATE INDEX IF NOT EXISTS idx_categories_parent_id ON categories (parent_id);

CREATE OR REPLACE FUNCTION categories_enforce_one_level() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    grandparent UUID;
BEGIN
    IF NEW.parent_id IS NULL THEN
        RETURN NEW;
    END IF;
    IF NEW.parent_id = NEW.id THEN
        RAISE EXCEPTION 'A category cannot be its own parent'
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'chk_categories_one_level';
    END IF;
    SELECT parent_id INTO grandparent
    FROM categories WHERE id = NEW.parent_id
    FOR SHARE;
    IF grandparent IS NOT NULL THEN
        RAISE EXCEPTION 'A subcategory cannot have subcategories'
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'chk_categories_one_level';
    END IF;
    IF EXISTS (SELECT 1 FROM categories WHERE parent_id = NEW.id) THEN
        RAISE EXCEPTION 'A category with subcategories cannot become one'
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'chk_categories_one_level';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_categories_one_level ON categories;

CREATE TRIGGER trg_categories_one_level
    BEFORE INSERT OR UPDATE OF parent_id ON categories
    FOR EACH ROW
    EXECUTE FUNCTION categories_enforce_one_level();

COMMENT ON COLUMN categories.parent_id IS
    'The top-level category this one is a subcategory of; NULL for a top-level category. One level deep (trg_categories_one_level).';
