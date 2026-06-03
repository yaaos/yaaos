import { ErrorBanner, PageHeader } from "@shared/components/layout";
import { Badge } from "@shared/components/ui/badge";
import { Button } from "@shared/components/ui/button";
import { Input } from "@shared/components/ui/input";
import { Label } from "@shared/components/ui/label";
import { Skeleton } from "@shared/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@shared/components/ui/table";
import { Suspense, useState } from "react";
import { ErrorBoundary } from "react-error-boundary";
import {
  type UserMembership,
  useClearGithubUsername,
  useUpdateDisplayName,
  useUpdateOrgHandle,
  useUserMe,
} from "./queries";

/**
 * `/orgs/$slug/user/details` — name + per-org handles + verified emails +
 * GitHub association. The GitHub username is written by the "Sign in with
 * GitHub" login flow; this page only displays it (and offers a Clear button).
 */
export function DetailsPage() {
  return (
    <ErrorBoundary
      fallbackRender={({ resetErrorBoundary }) => (
        <ErrorBanner message="Couldn't load your user profile." onRetry={resetErrorBoundary} />
      )}
    >
      <Suspense
        fallback={
          <div className="mx-auto flex max-w-[900px] flex-col gap-6 p-6">
            <Skeleton className="h-8 w-48" />
            <Skeleton className="h-32" />
          </div>
        }
      >
        <DetailsContent />
      </Suspense>
    </ErrorBoundary>
  );
}

function DetailsContent() {
  const { data } = useUserMe();

  return (
    <div className="mx-auto flex max-w-[900px] flex-col gap-6 p-6">
      <PageHeader
        title="Details"
        subtitle="Your profile, per-org handles, emails, and linked GitHub username."
      />
      <DisplayNameSection current={data.display_name} />
      <HandlesSection memberships={data.memberships} />
      <EmailsSection
        emails={data.emails.map((e) => ({
          email: e.email,
          is_primary: e.is_primary,
          verified: e.verified,
        }))}
      />
      <GithubSection username={data.github_username} />
    </div>
  );
}

function Section({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-lg border border-border bg-card">
      <header className="border-b border-border px-4 py-3">
        <h2 className="text-sm font-semibold">{title}</h2>
        {description && <p className="text-muted-foreground text-xs mt-1">{description}</p>}
      </header>
      <div className="px-4 py-4">{children}</div>
    </section>
  );
}

function DisplayNameSection({ current }: { current: string }) {
  const [value, setValue] = useState(current);
  const update = useUpdateDisplayName();
  const dirty = value !== current;
  return (
    <Section title="Display name">
      <div className="flex items-end gap-3">
        <div className="flex-1 flex flex-col gap-1.5">
          <Label htmlFor="display-name">Name shown to teammates</Label>
          <Input
            id="display-name"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            data-testid="display-name-input"
          />
        </div>
        <Button
          data-testid="display-name-save"
          disabled={update.isPending || !dirty}
          onClick={() => update.mutate(value)}
        >
          {update.isPending ? "Saving…" : "Save"}
        </Button>
      </div>
    </Section>
  );
}

function HandlesSection({ memberships }: { memberships: UserMembership[] }) {
  return (
    <Section
      title="Per-org handles"
      description="The handle other members of each org see when you act in their workspace."
    >
      {memberships.length === 0 ? (
        <p className="text-muted-foreground text-xs">No org memberships yet.</p>
      ) : (
        <Table data-testid="handles-table">
          <TableHeader>
            <TableRow>
              <TableHead>Org</TableHead>
              <TableHead>Role</TableHead>
              <TableHead>Handle</TableHead>
              <TableHead className="text-right" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {memberships.map((m) => (
              <HandleRow key={m.org_id} membership={m} />
            ))}
          </TableBody>
        </Table>
      )}
    </Section>
  );
}

function HandleRow({ membership }: { membership: UserMembership }) {
  const [value, setValue] = useState(membership.handle);
  const update = useUpdateOrgHandle();
  const dirty = value !== membership.handle;
  return (
    <TableRow>
      <TableCell className="font-medium">{membership.display_name || membership.slug}</TableCell>
      <TableCell>
        <Badge variant="secondary">{membership.role}</Badge>
      </TableCell>
      <TableCell>
        <Input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          data-testid={`handle-input-${membership.slug}`}
          className="h-8 w-[180px]"
        />
      </TableCell>
      <TableCell className="text-right">
        <Button
          size="sm"
          data-testid={`handle-save-${membership.slug}`}
          disabled={!dirty || update.isPending}
          onClick={() => update.mutate({ orgId: membership.org_id, handle: value })}
        >
          Save
        </Button>
        {update.isError && (
          <span
            className="ml-2 text-xs text-destructive"
            data-testid={`handle-err-${membership.slug}`}
          >
            {(update.error as Error)?.message || "Failed"}
          </span>
        )}
      </TableCell>
    </TableRow>
  );
}

function EmailsSection({
  emails,
}: {
  emails: { email: string; is_primary: boolean; verified: boolean }[];
}) {
  return (
    <Section title="Emails">
      {emails.length === 0 ? (
        <p className="text-muted-foreground text-xs">No emails on file.</p>
      ) : (
        <ul className="flex flex-col gap-2 text-sm" data-testid="emails-list">
          {emails.map((e) => (
            <li key={e.email} className="flex items-center gap-2">
              <span>{e.email}</span>
              {e.is_primary && <Badge>primary</Badge>}
              {e.verified ? (
                <Badge variant="secondary">verified</Badge>
              ) : (
                <Badge variant="destructive">unverified</Badge>
              )}
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

function GithubSection({ username }: { username: string | null }) {
  const clear = useClearGithubUsername();
  return (
    <Section
      title="GitHub association"
      description="Your GitHub handle is captured when you sign in with GitHub. Sign in again to refresh it."
    >
      {username ? (
        <div className="flex items-center gap-3">
          <span className="font-mono text-sm" data-testid="github-username">
            @{username}
          </span>
          <Badge variant="secondary">verified</Badge>
          <Button
            variant="destructive"
            size="sm"
            className="ml-auto"
            data-testid="github-clear"
            disabled={clear.isPending}
            onClick={() => clear.mutate()}
          >
            {clear.isPending ? "Clearing…" : "Clear"}
          </Button>
        </div>
      ) : (
        <p className="text-muted-foreground text-xs">
          No GitHub handle linked. Sign in with GitHub to populate.
        </p>
      )}
    </Section>
  );
}
