import { apiFetch } from "@core/api";
import { Badge, Button, Card, CardContent, CardHeader } from "@shared/components";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "@tanstack/react-router";
import { useState } from "react";

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
  return useQuery<Member[]>({
    queryKey: ["memberships", orgSlug],
    queryFn: () =>
      apiFetch<Member[]>("/api/memberships", {
        headers: orgSlug ? { "X-Org-Slug": orgSlug } : undefined,
      }),
    enabled: !!orgSlug,
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
          ? { "X-Org-Slug": orgSlug, "Content-Type": "application/json" }
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
          ? { "X-Org-Slug": orgSlug, "Content-Type": "application/json" }
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
        headers: orgSlug ? { "X-Org-Slug": orgSlug } : undefined,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["memberships", orgSlug] }),
  });
}

/**
 * Members page. Reads the current org slug from `X-Org-Slug` (Phase 7's
 * router-driven injection lands shortly). The interim contract: a parent
 * route component passes `orgSlug` as a prop. Until that route exists, the
 * page reads from `window.location` so devs can preview at `/members?org=...`.
 */
export function MembersPage(props: { orgSlug?: string }) {
  // Route is `/orgs/$slug/members` — params.slug carries the org slug.
  // Fallback to a prop or to `?org=` for ad-hoc preview at `/members`.
  const params = useParams({ strict: false }) as { slug?: string };
  const orgSlug =
    props.orgSlug ?? params.slug ?? new URLSearchParams(window.location.search).get("org");
  const { data, isLoading, error } = useMembers(orgSlug);
  const invite = useInvite(orgSlug);
  const changeRole = useChangeRole(orgSlug);
  const remove = useRemoveMember(orgSlug);

  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("builder");

  if (!orgSlug) {
    return (
      <div className="mx-auto max-w-[900px] p-6">
        No org selected. Append <code>?org=&lt;slug&gt;</code> to the URL.
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-[900px] flex flex-col gap-4 p-6">
      <h1 className="text-[20px] font-semibold tracking-tight">Members</h1>

      <Card>
        <CardHeader>
          <h2 className="font-semibold text-[13.5px]">Invite</h2>
        </CardHeader>
        <CardContent>
          <form
            className="flex gap-2 items-center"
            onSubmit={(e) => {
              e.preventDefault();
              if (!email) return;
              invite.mutate({ email, role });
              setEmail("");
            }}
          >
            <input
              type="email"
              required
              placeholder="email@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="flex-1 border rounded px-2 py-1 text-sm"
            />
            <select
              value={role}
              onChange={(e) => setRole(e.target.value as Role)}
              className="border rounded px-2 py-1 text-sm"
              data-testid="invite-role"
            >
              {ROLES.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
            <Button type="submit" disabled={invite.isPending}>
              {invite.isPending ? "Inviting…" : "Invite"}
            </Button>
          </form>
          {invite.isError && (
            <p className="text-red-500 text-xs mt-2">
              {(invite.error as Error)?.message ?? "Invite failed"}
            </p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <h2 className="font-semibold text-[13.5px]">Roster</h2>
        </CardHeader>
        <CardContent>
          {isLoading && <p className="text-text-3 text-xs">Loading…</p>}
          {error && (
            <p className="text-red-500 text-xs">{(error as Error).message ?? "Failed to load"}</p>
          )}
          {data && data.length === 0 && <p className="text-text-3 text-xs">No members yet.</p>}
          {data && data.length > 0 && (
            <table className="w-full text-sm">
              <thead className="text-text-3 text-[11.5px] uppercase">
                <tr>
                  <th className="text-left py-1">Member</th>
                  <th className="text-left py-1">Role</th>
                  <th className="text-right py-1">Actions</th>
                </tr>
              </thead>
              <tbody>
                {data.map((m) => (
                  <tr key={m.user_id} className="border-t">
                    <td className="py-2">
                      <div className="font-medium">{m.display_name || m.handle}</div>
                      <div className="text-text-3 text-xs">{m.primary_email}</div>
                    </td>
                    <td className="py-2">
                      <select
                        value={m.role}
                        onChange={(e) =>
                          changeRole.mutate({ user_id: m.user_id, role: e.target.value as Role })
                        }
                        className="border rounded px-2 py-1 text-xs"
                        data-testid={`role-${m.handle}`}
                      >
                        {ROLES.map((r) => (
                          <option key={r} value={r}>
                            {r}
                          </option>
                        ))}
                      </select>
                      {m.role === "owner" && <Badge variant="soft">owner</Badge>}
                    </td>
                    <td className="py-2 text-right">
                      <Button
                        variant="ghost"
                        onClick={() => {
                          if (window.confirm(`Remove ${m.handle}?`)) {
                            remove.mutate(m.user_id);
                          }
                        }}
                        data-testid={`remove-${m.handle}`}
                      >
                        Remove
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
