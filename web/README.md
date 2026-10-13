# depgraph web

Next.js (App Router) frontend for depgraph. See the [root README](../README.md) for setup
and [NOTES.md](../NOTES.md) (Stage 3) for design decisions.

```bash
npm ci
cp .env.example .env.local   # NEXT_PUBLIC_API_URL
npm run dev                  # http://localhost:3000
npm run gen:api              # regenerate src/lib/api/schema.d.ts from openapi.json
```
