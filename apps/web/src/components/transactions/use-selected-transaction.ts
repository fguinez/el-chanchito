"use client";

import { useCallback, type KeyboardEvent, type MouseEvent } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { hrefWithSearchParam, TXN_PARAM } from "@/lib/transaction-selection";

/**
 * The transaction open in the detail panel, kept in the `?txn=` param so the
 * panel survives reloads and can be linked. Callers need a Suspense boundary
 * (useSearchParams) when the page is statically prerendered.
 */
export function useSelectedTransaction() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const selectedTxnId = searchParams.get(TXN_PARAM);

  /** Opens (id) or closes (null) the detail panel, keeping other params. */
  const selectTransaction = useCallback(
    (id: string | null) => {
      router.replace(
        hrefWithSearchParam(pathname, searchParams.toString(), TXN_PARAM, id),
        { scroll: false }
      );
    },
    [router, pathname, searchParams]
  );

  /** Props that make a table row open `id` on click, Enter or Space. */
  const rowProps = useCallback(
    (id: string) => ({
      tabIndex: 0,
      "data-state": id === selectedTxnId ? "selected" : undefined,
      className:
        "cursor-pointer focus-visible:bg-muted/50 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring",
      onClick: (e: MouseEvent<HTMLTableRowElement>) => {
        // Let users select and copy cell text without opening.
        const sel = window.getSelection();
        if (
          sel?.toString() &&
          sel.anchorNode &&
          e.currentTarget.contains(sel.anchorNode)
        ) {
          return;
        }
        selectTransaction(id);
      },
      onKeyDown: (e: KeyboardEvent<HTMLTableRowElement>) => {
        if (e.target !== e.currentTarget) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          selectTransaction(id);
        }
      },
    }),
    [selectedTxnId, selectTransaction]
  );

  return { selectedTxnId, selectTransaction, rowProps };
}
