/** Turn API failures into messages a person can act on. */

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number | null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** FastAPI errors are `{detail: string}` or, for validation, `{detail: [{msg}]}`. */
export function detailOf(body: unknown): string | null {
  if (typeof body !== "object" || body === null) return null;
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const msgs = detail
      .map((d) => (typeof d === "object" && d && "msg" in d ? String(d.msg) : null))
      .filter(Boolean);
    return msgs.length ? msgs.join("; ") : null;
  }
  return null;
}

export function messageForStatus(status: number, body: unknown): string {
  const detail = detailOf(body);
  switch (status) {
    case 413:
      return "That lockfile is larger than the server accepts.";
    case 422:
      return detail ? `The server couldn't read that lockfile: ${detail}` : "The server couldn't read that lockfile.";
    case 429:
      return "Too many uploads from your network. Wait a minute and try again.";
    case 404:
      return detail ?? "Not found.";
    case 409:
      return detail ?? "That scan isn't ready yet.";
    default:
      if (status >= 500) return "The server hit an error. Try again in a moment.";
      return detail ?? `Request failed (HTTP ${status}).`;
  }
}

export const NETWORK_ERROR =
  "Couldn't reach the depgraph API. Check your connection, or the API may be starting up (try again in a few seconds).";
