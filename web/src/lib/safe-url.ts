/**
 * Advisory text and reference URLs are third-party content (malware reports
 * included), so only plain http(s) URLs ever become clickable links.
 */
export function safeHttpUrl(raw: string | null | undefined): string | null {
  if (!raw) return null;
  let url: URL;
  try {
    url = new URL(raw.trim());
  } catch {
    return null; // relative or malformed: never resolve against our own origin
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  if (url.username || url.password) return null; // https://bank.com@evil.example
  return url.href;
}

/** Attributes for every external link built from untrusted data. */
export const EXTERNAL_LINK = {
  target: "_blank",
  rel: "noopener noreferrer nofollow ugc",
} as const;
