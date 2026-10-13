import { Suspense } from "react";

import { ScanView } from "@/components/scan-view";

// Cache Components: the route param is request-time data, so it is read inside a
// <Suspense> boundary. The page shell prerenders; the scan streams in.
export default function ScanPage({ params }: PageProps<"/scans/[id]">) {
  return (
    <main className="mx-auto w-full max-w-5xl flex-1 px-4 py-10 sm:px-6">
      <Suspense fallback={null}>
        <ScanFromParams params={params} />
      </Suspense>
    </main>
  );
}

async function ScanFromParams({ params }: Pick<PageProps<"/scans/[id]">, "params">) {
  const { id } = await params;
  return <ScanView scanId={id} />;
}
