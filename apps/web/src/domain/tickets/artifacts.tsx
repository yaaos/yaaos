/**
 * Artifacts tab — one lineage per stage name, each with a version `Select`
 * ("v4 · run <pipeline> · 2d ago", loop drafts suffixed) and the rendered
 * markdown body for the selected version.
 */

import {
  type ArtifactGroupView,
  useArtifactVersion,
  useArtifacts,
  useRuns,
} from "@core/api/public/queries";
import { EmptyState } from "@shared/components/public/layout/empty-state";
import { Markdown } from "@shared/components/public/markdown";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@shared/components/ui/select";
import { Skeleton } from "@shared/components/ui/skeleton";
import { ago } from "@shared/utils/public/ago";
import { FileText } from "lucide-react";
import { useState } from "react";

export function ArtifactsTab({ ticketId }: { ticketId: string }) {
  const { data: groups } = useArtifacts(ticketId);
  const { data: runs } = useRuns(ticketId);
  const pipelineNameByRunId = new Map(runs.map((r) => [r.id, r.pipeline_name]));

  if (groups.length === 0) {
    return (
      <EmptyState
        icon={FileText}
        headline="No artifacts yet."
        body="Documents a pipeline stage produces appear here, versioned per stage."
      />
    );
  }

  return (
    <div className="flex flex-col gap-6">
      {groups.map((group) => (
        <ArtifactLineage
          key={group.stage_name}
          group={group}
          pipelineNameByRunId={pipelineNameByRunId}
        />
      ))}
    </div>
  );
}

function ArtifactLineage({
  group,
  pipelineNameByRunId,
}: {
  group: ArtifactGroupView;
  pipelineNameByRunId: Map<string, string>;
}) {
  const versions = [...group.versions].sort((a, b) => b.version - a.version);
  const [selectedId, setSelectedId] = useState(versions[0]?.id ?? "");
  const { data } = useArtifactVersion(selectedId || null);

  return (
    <section data-testid={`artifact-lineage-${group.stage_name}`}>
      <div className="flex items-center justify-between gap-2 mb-2">
        <h2 className="text-lg font-semibold">{group.stage_name}</h2>
        <Select value={selectedId} onValueChange={setSelectedId}>
          <SelectTrigger className="w-64 h-9">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {versions.map((v) => {
              const pipelineName = pipelineNameByRunId.get(v.run_id) ?? "run";
              const label = `v${v.version} · ${pipelineName} · ${ago(v.created_at)}${
                v.is_final ? "" : " (draft)"
              }`;
              return (
                <SelectItem key={v.id} value={v.id}>
                  {label}
                </SelectItem>
              );
            })}
          </SelectContent>
        </Select>
      </div>
      {data ? <Markdown>{data.body}</Markdown> : <Skeleton className="h-32" />}
    </section>
  );
}
