/**
 * Remembers each scan's dashboard filters for the tab session, so "← All findings"
 * on a detail page returns to the filtered view the user came from.
 * Best-effort: storage can be unavailable (privacy modes), which just means no memory.
 */
const key = (scanId: string) => `depgraph:filters:${scanId}`;

export function rememberFilters(scanId: string, query: string): void {
  try {
    if (query) sessionStorage.setItem(key(scanId), query);
    else sessionStorage.removeItem(key(scanId));
  } catch {
    /* storage unavailable */
  }
}

export function recallFilters(scanId: string): string {
  try {
    return sessionStorage.getItem(key(scanId)) ?? "";
  } catch {
    return "";
  }
}
