import { Suspense } from "react";
import RunView from "@/components/RunView";

/* With cacheComponents, route params are runtime data: read them inside a Suspense boundary. */
export default function RunPage({ params }: PageProps<"/run/[id]">) {
  return (
    <Suspense fallback={<RunSkeleton />}>
      <RunLoader params={params} />
    </Suspense>
  );
}

async function RunLoader({ params }: { params: PageProps<"/run/[id]">["params"] }) {
  const { id } = await params;
  return <RunView runId={decodeURIComponent(id)} />;
}

function RunSkeleton() {
  return (
    <div className="grid-bg flex h-full flex-col gap-3 p-3">
      <div className="glass shimmer h-16 rounded-2xl" />
      <div className="flex min-h-0 flex-1 gap-3">
        <div className="glass w-[40%] min-w-[420px] space-y-3 rounded-2xl p-4">
          <div className="shimmer h-24 rounded-xl" />
          <div className="shimmer h-32 rounded-xl" />
          <div className="shimmer h-20 rounded-xl" />
        </div>
        <div className="flex-1 rounded-2xl border border-white/[0.08] bg-[#070a10]" />
      </div>
    </div>
  );
}
