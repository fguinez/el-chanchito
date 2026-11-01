// The product page's Transacciones card as data: the movements a search and a
// credit card's billing filter keep, the page of them shown, and the copy
// around them (description, notes, empty state). Pure, so the component only
// renders it.

import {
  filterByBilling,
  type BillingFilter,
  type TransactionBilling,
} from "@/lib/card-billing";
import {
  matchesTransactionSearch,
  searchTokens,
} from "@/lib/transaction-search";

/** Rows rendered per "Ver más" step. */
export const PAGE_SIZE = 50;

/** What the user set on the list. */
export interface ListControls {
  /** The search box's text, as typed. */
  search: string;
  billingFilter: BillingFilter;
  /** How many matching rows to render. */
  visible: number;
}

export const INITIAL_LIST_CONTROLS: ListControls = {
  search: "",
  billingFilter: "all",
  visible: PAGE_SIZE,
};

export type ListAction =
  | { type: "search"; search: string }
  | { type: "billingFilter"; billingFilter: BillingFilter }
  | { type: "showMore" };

/** A new search or billing filter starts again from the first page; "Ver
 *  más" renders one more page. */
export function listControlsReducer(
  state: ListControls,
  action: ListAction
): ListControls {
  switch (action.type) {
    case "search":
      return { ...state, search: action.search, visible: PAGE_SIZE };
    case "billingFilter":
      return {
        ...state,
        billingFilter: action.billingFilter,
        visible: PAGE_SIZE,
      };
    case "showMore":
      return { ...state, visible: state.visible + PAGE_SIZE };
  }
}

/** What the list reads from each movement. */
export interface ListedTransaction {
  /** What the row shows (custom description or alias, else scraped). */
  description: string;
  /** The scraped text, which a search also matches. */
  rawDescription: string;
  billing: TransactionBilling | null;
}

export interface ProductTransactionsView<T> {
  total: number;
  /** The status column and the billing filter show: a credit card with a
   *  status on at least one movement. */
  showsBilling: boolean;
  /** The billing filter in effect: "all" while the list shows no status. */
  billingFilter: BillingFilter;
  searching: boolean;
  /** The movements both the search and the billing filter keep, newest
   *  first. */
  matching: readonly T[];
  /** The page of `matching` to render. */
  shown: readonly T[];
  /** The card's description: how many movements, and how many match. */
  description: string;
  /** Under the controls when the search or filter runs over a cut list. */
  coverageNote: string | null;
  /** Instead of the table, when nothing is stored or nothing matches. */
  emptyMessage: string | null;
  /** Next to "Ver más" while more matching rows remain. */
  pagingNote: string | null;
  /** A shown status is an estimate: explain the dashed border. */
  showsEstimateLegend: boolean;
}

/**
 * The Transacciones card for `rows` (newest first): a movement shows when it
 * matches every word of the search (lib/transaction-search) AND the billing
 * filter (filterByBilling). The billing filter applies only while the list
 * shows statuses: `showBilling` (a credit card) and a status on at least one
 * movement.
 */
export function productTransactionsView<T extends ListedTransaction>(input: {
  rows: readonly T[];
  /** The search text (deferred while typing). */
  search: string;
  billingFilter: BillingFilter;
  visible: number;
  showBilling: boolean;
  /** The endpoint cut the list: older movements were left out. */
  truncated: boolean;
}): ProductTransactionsView<T> {
  const { rows, truncated } = input;
  const total = rows.length;
  const tokens = searchTokens(input.search);
  const searching = tokens.length > 0;
  const showsBilling =
    input.showBilling && rows.some((row) => row.billing != null);
  const billingFilter: BillingFilter = showsBilling
    ? input.billingFilter
    : "all";
  const filtering = searching || billingFilter !== "all";

  const searched = searching
    ? rows.filter((row) =>
        matchesTransactionSearch(tokens, row.description, row.rawDescription)
      )
    : rows;
  const matching = filterByBilling(searched, billingFilter);
  const shown = matching.slice(0, input.visible);

  const noun = total === 1 ? "transacción" : "transacciones";
  const description =
    total === 0
      ? "Movimientos registrados para este producto"
      : truncated
        ? filtering
          ? `${matching.length} de las ${total} transacciones más recientes`
          : `Las ${total} transacciones más recientes`
        : filtering
          ? `${matching.length} de ${total} ${noun}`
          : `${total} ${noun}`;

  let emptyMessage: string | null = null;
  if (total === 0) {
    emptyMessage = "Sin transacciones registradas para este producto.";
  } else if (matching.length === 0) {
    emptyMessage = searching
      ? `Sin resultados para «${input.search.trim()}».`
      : billingFilter === "unbilled"
        ? "Sin movimientos no facturados."
        : "Sin movimientos facturados.";
  }

  return {
    total,
    showsBilling,
    billingFilter,
    searching,
    matching,
    shown,
    description,
    coverageNote:
      filtering && truncated
        ? `${searching ? "La búsqueda" : "El filtro"} cubre las ${total} transacciones más recientes.`
        : null,
    emptyMessage,
    pagingNote:
      matching.length > shown.length
        ? `Mostrando ${shown.length} de ${matching.length}${filtering ? " resultados" : ""}`
        : null,
    showsEstimateLegend:
      showsBilling && shown.some((row) => row.billing?.estimated),
  };
}
