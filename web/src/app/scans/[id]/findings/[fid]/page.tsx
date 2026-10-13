import { Suspense } from "react";

import { FindingDetailView } from "@/components/finding-detail";

// Route params are request-time data under Cache Components: read inside <Suspense>.
export default function FindingPage({ params }: PageProps<"/scans/[id]/findings/[fid]">) {
  return (
    <main className="mx-auto w-full max-w-4xl flex-1 px-4 py-10 sm:px-6">
      <Suspense fallback={null}>
        <FindingFromParams params={params} />
      </Suspense>
    </main>
  );
}

async function FindingFromParams({ params }: Pick<PageProps<"/scans/[id]/findings/[fid]">, "params">) {
  const { id, fid } = await params;
  return <FindingDetailView scanId={id} findingId={fid} />;
}
