// Client helpers shared by the institution and product edit dialogs.

/** Native <select> styled to match the Input primitive. */
export const SELECT_CLASS =
  "h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-xs transition-[color,box-shadow] outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-input/30";

export type SaveResult<T> = { ok: true; data: T } | { ok: false; error: string };

/**
 * PATCH `url` with a JSON body. Failures come back as a message to show: the
 * conflict messages in `conflicts` (keyed by the API's `field`) replace the
 * API's English 409 errors, anything else falls back to the API's own error.
 */
export async function patchJson<T>(
  url: string,
  body: Record<string, unknown>,
  conflicts: Record<string, string> = {}
): Promise<SaveResult<T>> {
  try {
    const res = await fetch(url, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => null);
    if (res.ok) return { ok: true, data: data as T };
    const field: string | undefined = data?.field;
    if (res.status === 409 && field && conflicts[field]) {
      return { ok: false, error: conflicts[field] };
    }
    return { ok: false, error: data?.error ?? "No se pudieron guardar los cambios" };
  } catch {
    return { ok: false, error: "No se pudieron guardar los cambios" };
  }
}
