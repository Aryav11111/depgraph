import { Suspense } from "react";

import { GraphView } from "@/components/graph-view";

// Route params are request-time data under Cache Components: read inside <Suspense>.
export default function GraphPage({ params }: PageProps<"/scans/[id]/graph">) {
  return (
    <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-8 sm:px-6">
      <Suspense fallback={null}>
        <GraphFromParams params={params} />
      </Suspense>
    </main>
  );
}

async function GraphFromParams({ params }: Pick<PageProps<"/scans/[id]/graph">, "params">) {
  const { id } = await params;
  return <GraphView scanId={id} />;
}
