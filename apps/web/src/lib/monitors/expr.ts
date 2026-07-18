// Expression language for monitors: arithmetic over product references.
//
// Grammar (whitespace-insensitive between tokens):
//   expr    := term (('+'|'-') term)*
//   term    := factor (('*'|'/') factor)*
//   factor  := '-' factor | primary
//   primary := NUMBER | ref | func | '(' expr ')'
//   func    := ('DAY_OF_MONTH' | 'DAYS_IN_MONTH') '(' ')'
//   ref     := uuidRef | slugRef
//   uuidRef := '@{' UUID ':' FIELD '}'      stored (persisted) form
//   slugRef := SLUG ':' SLUG ':' FIELD      display form: institution:product:field
//   NUMBER  := integer or decimal literal ('.' separator, no thousands sep)
//   SLUG    := [a-z0-9][a-z0-9_-]*    FIELD := [a-z][a-z0-9_]*
//
// A display ref is exactly three ':'-separated parts and is the only lowercase
// lexeme in the grammar (function names are uppercase and followed by '()'),
// so the parser tries the full slug-ref pattern before falling back to a
// number when it sees a digit.

import { METRIC_FIELDS, type ProductKind } from "@chanchito/product-model";

/** Parse/bind/serialize failure with a 0-based character position into the
 *  source, so callers (API 400s, the builder UI) can point at the exact spot. */
export class ExprError extends Error {
  readonly position: number;

  constructor(message: string, position: number) {
    super(message);
    this.name = "ExprError";
    this.position = position;
  }
}

export type NumberExpr = { type: "number"; value: number };

export type FuncName = "DAY_OF_MONTH" | "DAYS_IN_MONTH";
export type FuncExpr = { type: "func"; name: FuncName };

/** A product reference. A uuid ref has `productId` set; a display (slug) ref
 *  has the slug pair set and a null `productId` until bound via a catalog. */
export type RefExpr = {
  type: "ref";
  productId: string | null;
  institutionSlug: string | null;
  productSlug: string | null;
  field: string;
  /** 0-based offset of the ref in the parsed source, for error reporting. */
  position: number;
};

export type UnaryExpr = { type: "unary"; op: "-"; operand: Expr };

export type BinaryOp = "+" | "-" | "*" | "/";
export type BinaryExpr = {
  type: "binary";
  op: BinaryOp;
  left: Expr;
  right: Expr;
};

export type Expr = NumberExpr | FuncExpr | RefExpr | UnaryExpr | BinaryExpr;

/** The slice of a product the expression layer needs: identity + kind. The
 *  richer ProductInfo used by the evaluator satisfies this structurally. */
export type CatalogEntry = {
  id: string;
  kind: ProductKind;
  slug: string;
  institutionSlug: string;
};

export type ProductCatalog = {
  byId: Map<string, CatalogEntry>;
  /** Keyed by `${institutionSlug}:${slug}`. */
  bySlug: Map<string, CatalogEntry>;
};

const FUNC_NAMES: readonly FuncName[] = ["DAY_OF_MONTH", "DAYS_IN_MONTH"];

const SLUG_REF_RE = /([a-z0-9][a-z0-9_-]*):([a-z0-9][a-z0-9_-]*):([a-z][a-z0-9_]*)/y;
const NUMBER_RE = /[0-9]+(?:\.[0-9]+)?/y;
const UUID_RE = /[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/y;
const FIELD_RE = /[a-z][a-z0-9_]*/y;
const FUNC_NAME_RE = /[A-Z][A-Z0-9_]*/y;

class Parser {
  private pos = 0;

  constructor(private readonly src: string) {}

  parse(): Expr {
    this.skipWhitespace();
    if (this.pos >= this.src.length) {
      throw new ExprError("Empty expression", 0);
    }
    const expr = this.parseExpr();
    this.skipWhitespace();
    if (this.pos < this.src.length) {
      throw new ExprError(
        `Unexpected character '${this.src[this.pos]}'`,
        this.pos
      );
    }
    return expr;
  }

  private skipWhitespace(): void {
    while (this.pos < this.src.length && /\s/.test(this.src[this.pos])) {
      this.pos++;
    }
  }

  /** Run a sticky regex at the current position; advances on match. */
  private match(re: RegExp): RegExpExecArray | null {
    re.lastIndex = this.pos;
    const m = re.exec(this.src);
    if (m) this.pos = re.lastIndex;
    return m;
  }

  private parseExpr(): Expr {
    let left = this.parseTerm();
    for (;;) {
      this.skipWhitespace();
      const c = this.src[this.pos];
      if (c !== "+" && c !== "-") return left;
      this.pos++;
      const right = this.parseTerm();
      left = { type: "binary", op: c, left, right };
    }
  }

  private parseTerm(): Expr {
    let left = this.parseFactor();
    for (;;) {
      this.skipWhitespace();
      const c = this.src[this.pos];
      if (c !== "*" && c !== "/") return left;
      this.pos++;
      const right = this.parseFactor();
      left = { type: "binary", op: c, left, right };
    }
  }

  private parseFactor(): Expr {
    this.skipWhitespace();
    if (this.src[this.pos] === "-") {
      this.pos++;
      return { type: "unary", op: "-", operand: this.parseFactor() };
    }
    return this.parsePrimary();
  }

  private parsePrimary(): Expr {
    this.skipWhitespace();
    if (this.pos >= this.src.length) {
      throw new ExprError("Unexpected end of input", this.pos);
    }
    const c = this.src[this.pos];

    if (c === "(") {
      this.pos++;
      const inner = this.parseExpr();
      this.skipWhitespace();
      if (this.src[this.pos] !== ")") {
        throw new ExprError("Expected ')'", this.pos);
      }
      this.pos++;
      return inner;
    }

    if (c === "@") return this.parseUuidRef();
    if (/[A-Z]/.test(c)) return this.parseFunc();

    if (/[a-z0-9]/.test(c)) {
      // Slug refs may start with a digit, so try the full three-part pattern
      // before treating a leading digit as a number literal.
      const start = this.pos;
      const slugMatch = this.match(SLUG_REF_RE);
      if (slugMatch) {
        if (this.src[this.pos] === ":") {
          throw new ExprError(
            "Malformed product reference: expected institution:product:field",
            start
          );
        }
        return {
          type: "ref",
          productId: null,
          institutionSlug: slugMatch[1],
          productSlug: slugMatch[2],
          field: slugMatch[3],
          position: start,
        };
      }
      if (/[0-9]/.test(c)) {
        const numMatch = this.match(NUMBER_RE);
        if (numMatch) return { type: "number", value: Number(numMatch[0]) };
      }
      throw new ExprError(
        "Malformed product reference: expected institution:product:field",
        start
      );
    }

    throw new ExprError(`Unexpected character '${c}'`, this.pos);
  }

  private parseUuidRef(): RefExpr {
    const start = this.pos;
    const fail = (): never => {
      throw new ExprError(
        "Malformed product reference: expected @{uuid:field}",
        start
      );
    };
    this.pos++; // consume '@'
    if (this.src[this.pos] !== "{") fail();
    this.pos++;
    const uuid = this.match(UUID_RE) ?? fail();
    if (this.src[this.pos] !== ":") fail();
    this.pos++;
    const field = this.match(FIELD_RE) ?? fail();
    if (this.src[this.pos] !== "}") fail();
    this.pos++;
    return {
      type: "ref",
      productId: uuid[0].toLowerCase(),
      institutionSlug: null,
      productSlug: null,
      field: field[0],
      position: start,
    };
  }

  private parseFunc(): FuncExpr {
    const start = this.pos;
    const name = this.match(FUNC_NAME_RE)!;
    const funcName = FUNC_NAMES.find((f) => f === name[0]);
    if (!funcName) {
      throw new ExprError(`Unknown function '${name[0]}'`, start);
    }
    this.skipWhitespace();
    if (this.src[this.pos] !== "(") {
      throw new ExprError(`Expected '()' after ${funcName}`, this.pos);
    }
    this.pos++;
    this.skipWhitespace();
    if (this.src[this.pos] !== ")") {
      throw new ExprError(`Expected ')' to close ${funcName}()`, this.pos);
    }
    this.pos++;
    return { type: "func", name: funcName };
  }
}

/** Parse a monitor expression (uuid or display ref form, or a mix).
 *  Throws ExprError with a 0-based character position on invalid input. */
export function parseExpression(source: string): Expr {
  return new Parser(source).parse();
}

export type SerializeMode = "uuid" | "display";

const PRECEDENCE: Record<BinaryOp, number> = { "+": 1, "-": 1, "*": 2, "/": 2 };

function serializeRef(
  ref: RefExpr,
  mode: SerializeMode,
  catalog?: ProductCatalog
): string {
  if (mode === "uuid") {
    if (ref.productId == null) {
      throw new ExprError(
        `Cannot serialize unbound reference '${ref.institutionSlug}:${ref.productSlug}:${ref.field}' to uuid form`,
        ref.position
      );
    }
    return `@{${ref.productId}:${ref.field}}`;
  }
  // Display mode: resolve slugs through the catalog when the ref is bound;
  // fall back to slugs captured at parse time for unbound display refs.
  if (ref.productId != null) {
    const product = catalog?.byId.get(ref.productId);
    if (product) {
      return `${product.institutionSlug}:${product.slug}:${ref.field}`;
    }
  } else if (ref.institutionSlug != null && ref.productSlug != null) {
    return `${ref.institutionSlug}:${ref.productSlug}:${ref.field}`;
  }
  // Unresolvable id: keep the stored form so the breakage is visible (and the
  // string still round-trips through the parser).
  return `@{${ref.productId}:${ref.field}}`;
}

function serialize(
  expr: Expr,
  mode: SerializeMode,
  catalog?: ProductCatalog
): string {
  switch (expr.type) {
    case "number":
      return String(expr.value);
    case "func":
      return `${expr.name}()`;
    case "ref":
      return serializeRef(expr, mode, catalog);
    case "unary": {
      const inner = serialize(expr.operand, mode, catalog);
      return expr.operand.type === "binary" ? `-(${inner})` : `-${inner}`;
    }
    case "binary": {
      const prec = PRECEDENCE[expr.op];
      let left = serialize(expr.left, mode, catalog);
      if (expr.left.type === "binary" && PRECEDENCE[expr.left.op] < prec) {
        left = `(${left})`;
      }
      let right = serialize(expr.right, mode, catalog);
      // '-' and '/' are left-associative, so an equal-precedence right child
      // needs parens too: a - (b - c) is not a - b - c.
      if (
        expr.right.type === "binary" &&
        (PRECEDENCE[expr.right.op] < prec ||
          (PRECEDENCE[expr.right.op] === prec &&
            (expr.op === "-" || expr.op === "/")))
      ) {
        right = `(${right})`;
      }
      return `${left} ${expr.op} ${right}`;
    }
  }
}

/**
 * Render an AST back to source. `'uuid'` (the stored form) requires every ref
 * to be bound; `'display'` maps product ids to `institution:product:field`
 * via the catalog, keeping unresolvable ids in the `@{uuid:field}` form so
 * broken references stay recognizable.
 */
export function serializeExpression(
  expr: Expr,
  mode: SerializeMode,
  catalog?: ProductCatalog
): string {
  return serialize(expr, mode, catalog);
}

/** All product references in the expression, in source order. */
export function collectRefs(expr: Expr): RefExpr[] {
  switch (expr.type) {
    case "number":
    case "func":
      return [];
    case "ref":
      return [expr];
    case "unary":
      return collectRefs(expr.operand);
    case "binary":
      return [...collectRefs(expr.left), ...collectRefs(expr.right)];
  }
}

/**
 * Resolve display (slug) refs to product uuids. Already-bound refs pass
 * through untouched. Throws ExprError at the ref's source position when the
 * institution:product pair is unknown.
 */
export function bindExpression(expr: Expr, catalog: ProductCatalog): Expr {
  switch (expr.type) {
    case "number":
    case "func":
      return expr;
    case "ref": {
      if (expr.productId != null) return expr;
      const key = `${expr.institutionSlug}:${expr.productSlug}`;
      const product = catalog.bySlug.get(key);
      if (!product) {
        throw new ExprError(`Unknown product '${key}'`, expr.position);
      }
      return { ...expr, productId: product.id };
    }
    case "unary":
      return { ...expr, operand: bindExpression(expr.operand, catalog) };
    case "binary":
      return {
        ...expr,
        left: bindExpression(expr.left, catalog),
        right: bindExpression(expr.right, catalog),
      };
  }
}

export type ValidationIssue = { message: string; position: number };

function refDescription(ref: RefExpr): string {
  if (ref.institutionSlug != null && ref.productSlug != null) {
    return `${ref.institutionSlug}:${ref.productSlug}`;
  }
  return `${ref.productId}`;
}

/**
 * Check every reference against the catalog: the product must exist and the
 * field must be referencable for its kind (`current_balance` is universal;
 * everything else must be a numeric metric in METRIC_FIELDS[kind]).
 */
export function validateExpression(
  expr: Expr,
  catalog: ProductCatalog
): ValidationIssue[] {
  const issues: ValidationIssue[] = [];
  for (const ref of collectRefs(expr)) {
    const product =
      ref.productId != null
        ? catalog.byId.get(ref.productId)
        : catalog.bySlug.get(`${ref.institutionSlug}:${ref.productSlug}`);
    if (!product) {
      issues.push({
        message: `Unknown product '${refDescription(ref)}'`,
        position: ref.position,
      });
      continue;
    }
    if (
      ref.field !== "current_balance" &&
      !(ref.field in METRIC_FIELDS[product.kind])
    ) {
      issues.push({
        message: `Field '${ref.field}' is not valid for kind '${product.kind}'`,
        position: ref.position,
      });
    }
  }
  return issues;
}
