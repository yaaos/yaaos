/**
 * Org picker.
 *
 * Sparse landing for multi-org users: card per org with role badge, click
 * to enter, plus a "Create new organization" modal.
 *
 * Data sources:
 *   - useMyOrgs()  → GET /api/orgs/mine (the cross-org list)
 *   - useCreateOrg() → POST /api/orgs (the picker's "Create" modal target)
 *
 * `last_used_at` is null today; the card omits the timestamp.
 * Alphabetical sort by slug is the only reasonable order without it.
 */

import { type MineOrg, useCreateOrg, useMyOrgs } from "@core/api";
import { EmptyState, ErrorBanner, PageHeader } from "@shared/components/layout";
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
import { Skeleton } from "@shared/components/ui/skeleton";
import { cn } from "@shared/utils/cn";
import { Link } from "@tanstack/react-router";
import { Building2, Plus } from "lucide-react";
import { Suspense, useState } from "react";
import { ErrorBoundary } from "react-error-boundary";

const ROLE_LABEL: Record<MineOrg["role"], { label: string; chip: string }> = {
  owner: { label: "Owner", chip: "bg-primary/10 text-primary border-primary/30" },
  admin: { label: "Admin", chip: "bg-info/15 text-info border-info/30" },
  builder: { label: "Builder", chip: "bg-secondary text-muted-foreground border-border" },
};

export function OrgPickerPage() {
  const [showCreate, setShowCreate] = useState(false);

  return (
    <div className="mx-auto max-w-[700px] px-6 py-8">
      <PageHeader
        title="Your organizations"
        subtitle="Pick one to keep working."
        actions={
          <Button onClick={() => setShowCreate(true)} data-testid="org-picker-create">
            <Plus className="w-3.5 h-3.5" />
            Create
          </Button>
        }
      />

      <ErrorBoundary
        fallbackRender={({ resetErrorBoundary }) => (
          <ErrorBanner message="Couldn't load organizations." onRetry={resetErrorBoundary} />
        )}
      >
        <Suspense
          fallback={
            <div className="flex flex-col gap-2">
              {Array.from({ length: 3 }).map((_, i) => (
                // biome-ignore lint/suspicious/noArrayIndexKey: skeletons
                <Skeleton key={i} className="h-14" />
              ))}
            </div>
          }
        >
          <OrgList onCreateClick={() => setShowCreate(true)} />
        </Suspense>
      </ErrorBoundary>

      <CreateOrgModal open={showCreate} onOpenChange={setShowCreate} />
    </div>
  );
}

function OrgList({ onCreateClick }: { onCreateClick: () => void }) {
  const { data: orgs } = useMyOrgs();

  if (orgs.length === 0) {
    return (
      <EmptyState
        icon={Building2}
        headline="You don't belong to any organizations yet."
        body="Ask an admin to invite your email, or create one yourself."
        action={
          <Button onClick={onCreateClick}>
            <Plus className="w-3.5 h-3.5" />
            Create your first org
          </Button>
        }
      />
    );
  }

  return (
    <ul className="flex flex-col gap-2" data-testid="org-picker-list">
      {orgs.map((o) => (
        <li key={o.slug}>
          <Link
            to="/orgs/$slug/dashboard"
            params={{ slug: o.slug }}
            data-testid={`org-picker-row-${o.slug}`}
            className="flex items-center gap-3 px-4 py-3 rounded-md border border-border hover:bg-accent hover:text-accent-foreground transition-colors"
          >
            <Building2 className="w-4 h-4 shrink-0 text-muted-foreground" />
            <div className="flex-1 min-w-0">
              <div className="font-medium text-sm truncate">{o.name || o.slug}</div>
              <div className="text-xs text-muted-foreground mono">{o.slug}</div>
            </div>
            <span
              className={cn(
                "inline-flex items-center px-1.5 h-5 rounded text-[10.5px] font-medium border",
                ROLE_LABEL[o.role].chip,
              )}
            >
              {ROLE_LABEL[o.role].label}
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

function CreateOrgModal({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const create = useCreateOrg();
  const reset = () => {
    setName("");
    setSlug("");
    create.reset();
  };
  const submit = () => {
    const n = name.trim();
    const s = slug.trim().toLowerCase();
    if (!n || !s) return;
    create.mutate(
      { name: n, slug: s },
      {
        onSuccess: (resp) => {
          onOpenChange(false);
          reset();
          window.location.href = `/orgs/${resp.slug}/dashboard`;
        },
      },
    );
  };
  const errorMessage = (create.error as Error | null)?.message;
  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        onOpenChange(v);
        if (!v) reset();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Create organization</DialogTitle>
          <DialogDescription>
            A short slug becomes the URL prefix: <span className="mono">/orgs/&lt;slug&gt;</span>.
            You'll start as Admin.
          </DialogDescription>
        </DialogHeader>
        <form
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <div className="flex flex-col gap-1">
            <label className="text-xs font-medium text-muted-foreground" htmlFor="create-org-name">
              Display name
            </label>
            <Input
              id="create-org-name"
              data-testid="create-org-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoFocus
              required
            />
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-xs font-medium text-muted-foreground" htmlFor="create-org-slug">
              Slug
            </label>
            <Input
              id="create-org-slug"
              data-testid="create-org-slug"
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              placeholder="acme-team"
              required
              pattern="[a-z0-9-]+"
            />
            <p className="text-xs text-muted-foreground">Lowercase letters, digits, hyphens.</p>
          </div>
          {errorMessage && (
            <p className="text-xs text-destructive" data-testid="create-org-error">
              {/slug_taken/.test(errorMessage)
                ? "That slug is already taken."
                : /invalid_slug/.test(errorMessage)
                  ? "Slug must be lowercase a–z, digits, or hyphens."
                  : errorMessage}
            </p>
          )}
        </form>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={create.isPending}>
            Cancel
          </Button>
          <Button
            onClick={submit}
            disabled={create.isPending || !name.trim() || !slug.trim()}
            data-testid="create-org-submit"
          >
            {create.isPending ? "Creating…" : "Create"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
