import { apiFetch, getCurrentOrgSlug } from "@core/api";
import { ErrorBanner, PageHeader } from "@shared/components/layout";
import { Input } from "@shared/components/ui/input";
import { Label } from "@shared/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@shared/components/ui/select";
import { Skeleton } from "@shared/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@shared/components/ui/table";
import { useSuspenseQuery } from "@tanstack/react-query";
import { Suspense, useState } from "react";
import { ErrorBoundary } from "react-error-boundary";

interface AuditRow {
  id: string;
  entity_kind: string;
  entity_id: string;
  kind: string;
  payload: Record<string, unknown>;
  actor_kind: string;
  actor_user_id: string | null;
  actor_login: string | null;
  created_at: string;
}

function useAudit(filters: { actor_kind?: string; action?: string }) {
  const slug = getCurrentOrgSlug();
  const params = new URLSearchParams();
  if (filters.actor_kind) params.set("actor_kind", filters.actor_kind);
  if (filters.action) params.set("action", filters.action);
  return useSuspenseQuery<AuditRow[]>({
    queryKey: ["audit", slug, filters],
    queryFn: () =>
      slug ? apiFetch<AuditRow[]>(`/api/audit?${params.toString()}`) : Promise.resolve([]),
  });
}

/**
 * Owner/Admin-only org audit feed. Server-side `require(AUDIT_READ)`
 * enforces Admin minimum; the UI doesn't pre-filter — a Builder who
 * navigates here just sees a 403.
 */
export function AuditPage() {
  const [actorKind, setActorKind] = useState("");
  const [action, setAction] = useState("");

  return (
    <div className="mx-auto max-w-[1100px] flex flex-col gap-4 p-6">
      <PageHeader title="Audit" subtitle="Mutating-action log scoped to this org." />
      <section className="rounded-lg border border-border bg-card">
        <header className="border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold">Filters</h2>
        </header>
        <div className="px-4 py-4 flex flex-wrap items-end gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="audit-actor">Actor</Label>
            <Select
              value={actorKind || "all"}
              onValueChange={(v) => setActorKind(v === "all" ? "" : v)}
            >
              <SelectTrigger id="audit-actor" className="w-[160px]">
                <SelectValue placeholder="All" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">all</SelectItem>
                <SelectItem value="user">user</SelectItem>
                <SelectItem value="workspace">workspace</SelectItem>
                <SelectItem value="system">system</SelectItem>
                <SelectItem value="sso">sso</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="audit-action">Action</Label>
            <Input
              id="audit-action"
              value={action}
              onChange={(e) => setAction(e.target.value)}
              placeholder="e.g. invited"
              className="w-[200px]"
            />
          </div>
        </div>
      </section>

      <ErrorBoundary
        fallbackRender={({ resetErrorBoundary }) => (
          <ErrorBanner message="Couldn't load audit log." onRetry={resetErrorBoundary} />
        )}
      >
        <Suspense fallback={<Skeleton className="h-48" />}>
          <AuditTable
            filters={{ actor_kind: actorKind || undefined, action: action || undefined }}
          />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}

function AuditTable({ filters }: { filters: { actor_kind?: string; action?: string } }) {
  const { data } = useAudit(filters);

  return (
    <section className="rounded-lg border border-border bg-card">
      <div className="px-4 py-4">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Time</TableHead>
              <TableHead>Actor</TableHead>
              <TableHead>Action</TableHead>
              <TableHead>Entity</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {data.map((r) => (
              <TableRow key={r.id}>
                <TableCell className="font-mono text-xs">{r.created_at}</TableCell>
                <TableCell>
                  {r.actor_kind}
                  {r.actor_login ? ` (${r.actor_login})` : ""}
                </TableCell>
                <TableCell className="font-mono">{r.kind}</TableCell>
                <TableCell className="font-mono text-xs">
                  {r.entity_kind}:{r.entity_id.slice(0, 8)}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </section>
  );
}
