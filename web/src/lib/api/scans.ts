/** Scan-related API calls, with failures turned into readable `ApiError`s. */
import { api, type Scan } from "./client";
import { ApiError, messageForStatus, NETWORK_ERROR } from "./errors";

async function call<T>(
  request: () => Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  let result: Awaited<ReturnType<typeof request>>;
  try {
    result = await request();
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(NETWORK_ERROR, null);
  }
  if (result.data !== undefined) return result.data;
  throw new ApiError(messageForStatus(result.response.status, result.error), result.response.status);
}

/**
 * Upload a lockfile. The file is sent byte-for-byte as the request body (the API
 * takes the raw package-lock.json), so `bodySerializer` passes it through untouched
 * instead of JSON-encoding it.
 */
export function createScan(lockfile: Blob, signal?: AbortSignal): Promise<Scan> {
  return call(() =>
    api.POST("/scans", {
      body: lockfile as unknown as Record<string, never>,
      bodySerializer: (body) => body as unknown as BodyInit,
      headers: { "Content-Type": "application/json" },
      signal,
    }),
  );
}

export function getScan(scanId: string, signal?: AbortSignal): Promise<Scan> {
  return call(() => api.GET("/scans/{scan_id}", { params: { path: { scan_id: scanId } }, signal }));
}

export function getFindings(scanId: string, signal?: AbortSignal) {
  return call(() =>
    api.GET("/scans/{scan_id}/findings", { params: { path: { scan_id: scanId } }, signal }),
  );
}

export function getFinding(scanId: string, findingId: number, signal?: AbortSignal) {
  return call(() =>
    api.GET("/scans/{scan_id}/findings/{finding_id}", {
      params: { path: { scan_id: scanId, finding_id: findingId } },
      signal,
    }),
  );
}
