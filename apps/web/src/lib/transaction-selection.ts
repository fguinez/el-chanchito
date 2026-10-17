/** Query param holding the transaction shown in the detail panel. */
export const TXN_PARAM = "txn";

/**
 * `pathname` plus `search` with `key` set to `value` (removed when null),
 * keeping every other param. No trailing "?" when nothing is left.
 */
export function hrefWithSearchParam(
  pathname: string,
  search: string,
  key: string,
  value: string | null
): string {
  const params = new URLSearchParams(search);
  if (value) params.set(key, value);
  else params.delete(key);
  const query = params.toString();
  return query ? `${pathname}?${query}` : pathname;
}

/**
 * The id a transaction list shows: the source dedup key (the institution's
 * operation id for some sources) when there is one, otherwise our internal
 * uuid.
 */
export function transactionDisplayId(txn: {
  id: string;
  externalId: string | null;
}): { value: string; external: boolean } {
  return txn.externalId
    ? { value: txn.externalId, external: true }
    : { value: txn.id, external: false };
}
