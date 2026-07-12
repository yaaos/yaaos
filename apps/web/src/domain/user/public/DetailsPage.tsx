import { ErrorBanner } from "@shared/components/public/layout/error-banner";
import { PageHeader } from "@shared/components/public/layout/page-header";
import { Badge } from "@shared/components/ui/badge";
import { Button } from "@shared/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@shared/components/ui/dialog";
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
import { useQueryClient } from "@tanstack/react-query";
import { Suspense, useEffect, useState } from "react";
import { ErrorBoundary } from "react-error-boundary";
import {
  type OAuthConnectionView,
  type UserMembership,
  useClearGithubUsername,
  useDisconnectOAuth,
  useOAuthConnections,
  usePollDeviceAuth,
  useStartDeviceAuth,
  useUpdateDisplayName,
  useUpdateOrgHandle,
  useUserMe,
} from "./queries";

/**
 * `/org/$slug/user/details` — name + per-org handles + verified emails +
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
      <ErrorBoundary
        fallbackRender={({ resetErrorBoundary }) => (
          <ErrorBanner message="Couldn't load connections." onRetry={resetErrorBoundary} />
        )}
      >
        <Suspense fallback={<Skeleton className="h-24 rounded-lg" />}>
          <ConnectionsSection />
        </Suspense>
      </ErrorBoundary>
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

// ---------------------------------------------------------------------------
// Connections section — OAuth user connections
// ---------------------------------------------------------------------------

function ConnectionsSection() {
  const { data: connections } = useOAuthConnections();

  if (connections.length === 0) return null;

  return (
    <Section
      title="Connections"
      description="Connect third-party accounts for coding agent integrations."
    >
      <div className="flex flex-col gap-3" data-testid="connections-section">
        {connections.map((c) => (
          <ConnectionCard key={c.provider_id} connection={c} />
        ))}
      </div>
    </Section>
  );
}

function ConnectionCard({ connection }: { connection: OAuthConnectionView }) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [polling, setPolling] = useState(false);
  const [disconnectOpen, setDisconnectOpen] = useState(false);

  const qc = useQueryClient();
  const startMutation = useStartDeviceAuth(connection.provider_id);
  const disconnectMutation = useDisconnectOAuth(connection.provider_id);
  const pollQuery = usePollDeviceAuth(connection.provider_id, polling);

  // On grant: close dialog, stop polling, refresh connections list.
  useEffect(() => {
    if (pollQuery.data?.status === "connected") {
      setPolling(false);
      setDialogOpen(false);
      void qc.invalidateQueries({ queryKey: ["user-oauth-connections"] });
    } else if (pollQuery.data?.status === "denied" || pollQuery.data?.status === "expired") {
      setPolling(false);
    }
  }, [pollQuery.data?.status, qc]);

  function handleConnect() {
    startMutation.mutate(undefined, {
      onSuccess: () => {
        setDialogOpen(true);
        setPolling(true);
      },
    });
  }

  function handleDisconnectConfirm() {
    disconnectMutation.mutate(undefined, {
      onSuccess: () => setDisconnectOpen(false),
    });
  }

  const isConnected = connection.status === "connected";
  const needsReauth = connection.status === "needs_reauth";
  const startData = startMutation.data;
  const pollStatus = pollQuery.data?.status;

  return (
    <div
      className="flex items-center justify-between gap-3 rounded-md border border-border p-3"
      data-testid={`connection-row-${connection.provider_id}`}
    >
      <div className="flex flex-col gap-0.5">
        <span className="text-sm font-medium">{connection.display_name}</span>
        {isConnected && connection.external_account_id && (
          <span className="text-muted-foreground text-xs">
            Connected: {connection.external_account_id}
          </span>
        )}
        {needsReauth && (
          <span className="text-destructive text-xs">
            {connection.needs_reauth_reason || "Re-authorization required."}
          </span>
        )}
      </div>
      <div className="flex items-center gap-2">
        {isConnected ? (
          <>
            <Badge variant="secondary">Connected</Badge>
            <Button
              variant="destructive"
              size="sm"
              data-testid={`connection-disconnect-${connection.provider_id}`}
              disabled={disconnectMutation.isPending}
              onClick={() => setDisconnectOpen(true)}
            >
              Disconnect
            </Button>
          </>
        ) : (
          <Button
            size="sm"
            data-testid={`connection-connect-${connection.provider_id}`}
            disabled={startMutation.isPending}
            onClick={handleConnect}
          >
            {startMutation.isPending ? "Starting…" : "Connect"}
          </Button>
        )}
      </div>

      {/* Device-auth dialog */}
      <Dialog
        open={dialogOpen}
        onOpenChange={(open) => {
          if (!open) {
            setPolling(false);
          }
          setDialogOpen(open);
        }}
      >
        <DialogContent data-testid="device-auth-dialog">
          <DialogHeader>
            <DialogTitle>Connect {connection.display_name}</DialogTitle>
            <DialogDescription>{connection.connect_hint}</DialogDescription>
          </DialogHeader>
          {startData && (
            <div className="flex flex-col gap-4 py-2">
              <div className="flex flex-col gap-1">
                <span className="text-xs text-muted-foreground">Visit this URL:</span>
                <a
                  href={startData.verification_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-sm font-mono text-primary underline break-all"
                  data-testid="device-auth-verification-url"
                >
                  {startData.verification_url}
                </a>
              </div>
              <div className="flex flex-col gap-1">
                <span className="text-xs text-muted-foreground">Enter this code:</span>
                <span
                  className="font-mono text-2xl font-bold tracking-widest text-center py-2 rounded bg-muted"
                  data-testid="device-auth-user-code"
                >
                  {startData.user_code}
                </span>
              </div>
              <p className="text-xs text-muted-foreground text-center">
                {pollStatus === "pending" && "Waiting for authorization…"}
                {pollStatus === "denied" && "Authorization denied."}
                {pollStatus === "expired" && "Code expired. Close and try again."}
                {!pollStatus && "Polling…"}
              </p>
            </div>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setPolling(false);
                setDialogOpen(false);
              }}
            >
              Cancel
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Disconnect confirm dialog */}
      <Dialog open={disconnectOpen} onOpenChange={setDisconnectOpen}>
        <DialogContent data-testid="disconnect-confirm">
          <DialogHeader>
            <DialogTitle>Disconnect {connection.display_name}?</DialogTitle>
            <DialogDescription>This can't be undone.</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDisconnectOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              data-testid="disconnect-confirm-action"
              disabled={disconnectMutation.isPending}
              onClick={handleDisconnectConfirm}
            >
              {disconnectMutation.isPending ? "Disconnecting…" : "Disconnect"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
