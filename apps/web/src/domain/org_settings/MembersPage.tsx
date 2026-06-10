import { apiFetch } from "@core/api/public/client";
import { ErrorBanner } from "@shared/components/public/layout/error-banner";
import { PageHeader } from "@shared/components/public/layout/page-header";
import { Badge } from "@shared/components/ui/badge";
import { Button } from "@shared/components/ui/button";
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
import { useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { useParams } from "@tanstack/react-router";
import { Suspense, useState } from "react";
import { ErrorBoundary } from "react-error-boundary";

type Role = "owner" | "admin" | "builder";

interface Member {
  user_id: string;
  handle: string;
  role: Role;
  display_name: string;
  primary_email: string | null;
}

const ROLES: Role[] = ["owner", "admin", "builder"];

function useMembers(orgSlug: string | null) {
  return useSuspenseQuery<Member[]>({
    queryKey: ["memberships", orgSlug],
    queryFn: () =>
      orgSlug
        ? apiFetch<Member[]>("/api/memberships", {
            headers: { "X-Yaaos-Org-Slug": orgSlug },
          })
        : Promise.resolve([]),
  });
}

function useInvite(orgSlug: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { email: string; role: Role }) =>
      apiFetch("/api/memberships/invite", {
        method: "POST",
        body: JSON.stringify(body),
        headers: orgSlug
          ? { "X-Yaaos-Org-Slug": orgSlug, "Content-Type": "application/json" }
          : { "Content-Type": "application/json" },
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["memberships", orgSlug] }),
  });
}

function useChangeRole(orgSlug: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ user_id, role }: { user_id: string; role: Role }) =>
      apiFetch(`/api/memberships/${user_id}`, {
        method: "PATCH",
        body: JSON.stringify({ role }),
        headers: orgSlug
          ? { "X-Yaaos-Org-Slug": orgSlug, "Content-Type": "application/json" }
          : { "Content-Type": "application/json" },
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["memberships", orgSlug] }),
  });
}

function useRemoveMember(orgSlug: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (user_id: string) =>
      apiFetch(`/api/memberships/${user_id}`, {
        method: "DELETE",
        headers: orgSlug ? { "X-Yaaos-Org-Slug": orgSlug } : undefined,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["memberships", orgSlug] }),
  });
}

/**
 * Members page. Reads the current org slug from the route params; ad-hoc
 * preview supported via `?org=<slug>`.
 */
export function MembersPage(props: { orgSlug?: string }) {
  const params = useParams({ strict: false }) as { slug?: string };
  const orgSlug =
    props.orgSlug ?? params.slug ?? new URLSearchParams(window.location.search).get("org");

  if (!orgSlug) {
    return (
      <div className="mx-auto max-w-[900px] p-6 text-sm">
        No org selected. Append <code className="font-mono">?org=&lt;slug&gt;</code> to the URL.
      </div>
    );
  }

  return (
    <ErrorBoundary
      fallbackRender={({ resetErrorBoundary }) => (
        <ErrorBanner message="Couldn't load members." onRetry={resetErrorBoundary} />
      )}
    >
      <Suspense
        fallback={
          <div className="mx-auto max-w-[900px] flex flex-col gap-4 p-6">
            <Skeleton className="h-8 w-32" />
            <Skeleton className="h-48" />
          </div>
        }
      >
        <MembersContent orgSlug={orgSlug} />
      </Suspense>
    </ErrorBoundary>
  );
}

function MembersContent({ orgSlug }: { orgSlug: string }) {
  const { data } = useMembers(orgSlug);
  const invite = useInvite(orgSlug);
  const changeRole = useChangeRole(orgSlug);
  const remove = useRemoveMember(orgSlug);

  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("builder");

  return (
    <div className="mx-auto max-w-[900px] flex flex-col gap-4 p-6">
      <PageHeader title="Members" subtitle="Roster + invitations for this org." />

      <section className="rounded-lg border border-border bg-card">
        <header className="border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold">Invite</h2>
        </header>
        <div className="px-4 py-4">
          <form
            className="flex flex-wrap items-end gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              if (!email) return;
              invite.mutate({ email, role });
              setEmail("");
            }}
          >
            <div className="flex-1 min-w-[200px] flex flex-col gap-1.5">
              <Label htmlFor="invite-email">Email</Label>
              <Input
                id="invite-email"
                type="email"
                required
                placeholder="email@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="invite-role">Role</Label>
              <Select value={role} onValueChange={(v) => setRole(v as Role)}>
                <SelectTrigger id="invite-role" data-testid="invite-role" className="w-[140px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {ROLES.map((r) => (
                    <SelectItem key={r} value={r}>
                      {r}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <Button type="submit" disabled={invite.isPending}>
              {invite.isPending ? "Inviting…" : "Invite"}
            </Button>
          </form>
          {invite.isError && (
            <p className="text-destructive text-xs mt-2">
              {(invite.error as Error)?.message ?? "Invite failed"}
            </p>
          )}
        </div>
      </section>

      <section className="rounded-lg border border-border bg-card">
        <header className="border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold">Roster</h2>
        </header>
        <div className="px-4 py-4">
          {data.length === 0 && <p className="text-muted-foreground text-xs">No members yet.</p>}
          {data.length > 0 && (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Member</TableHead>
                  <TableHead>Role</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.map((m) => (
                  <TableRow key={m.user_id}>
                    <TableCell>
                      <div className="font-medium">{m.display_name || m.handle}</div>
                      <div className="text-muted-foreground text-xs">{m.primary_email}</div>
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center gap-2">
                        <Select
                          value={m.role}
                          onValueChange={(v) =>
                            changeRole.mutate({ user_id: m.user_id, role: v as Role })
                          }
                        >
                          <SelectTrigger data-testid={`role-${m.handle}`} className="h-8 w-[110px]">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            {ROLES.map((r) => (
                              <SelectItem key={r} value={r}>
                                {r}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                        {m.role === "owner" && <Badge variant="secondary">owner</Badge>}
                      </div>
                    </TableCell>
                    <TableCell className="text-right">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          if (window.confirm(`Remove ${m.handle}?`)) {
                            remove.mutate(m.user_id);
                          }
                        }}
                        data-testid={`remove-${m.handle}`}
                      >
                        Remove
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </div>
      </section>
    </div>
  );
}
