/**
 * Typed client for the depgraph API.
 *
 * `schema.d.ts` is generated from the FastAPI OpenAPI schema (`npm run gen:api`),
 * so every path, query parameter and response body below is checked by the
 * compiler: rename a field in the backend and the frontend stops compiling.
 *
 * The browser calls the API directly (CORS), not through a Next.js proxy:
 * Vercel caps proxied request bodies around 4.5 MB, below real lockfile sizes.
 */
import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

// Inlined at build time (NEXT_PUBLIC_*): each deployment is built with its API URL.
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8080";

export const api = createClient<paths>({ baseUrl: API_URL });

export type Schemas = components["schemas"];
export type Scan = Schemas["ScanOut"];
export type FindingSummary = Schemas["FindingSummary"];
export type FindingDetail = Schemas["FindingDetail"];
export type Graph = Schemas["GraphOut"];
export type Severity = FindingSummary["advisory"]["severity"];
