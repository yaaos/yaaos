import { Button } from "@shared/components/ui/button";
import { Input } from "@shared/components/ui/input";
import { Label } from "@shared/components/ui/label";
import { useState } from "react";
import { SsoConfigPage } from "../orgs/SsoConfigPage";
import { OrgSettingsLayout } from "./OrgSettingsLayout";
import { useUpdateOrgSettings } from "./queries";

/**
 * Org Settings > Auth: SSO config UI + session-timeout override
 * editor. Owner+Admin only.
 */
export function AuthSettingsPage() {
  return (
    <OrgSettingsLayout active="auth">
      <div className="mx-auto flex max-w-[900px] flex-col gap-4 p-6">
        <SessionTimeoutCard />
        <SsoConfigPage />
      </div>
    </OrgSettingsLayout>
  );
}

function SessionTimeoutCard() {
  const [minutes, setMinutes] = useState<string>("");
  const update = useUpdateOrgSettings();

  const onSave = () => {
    const parsed = minutes.trim() === "" ? null : Number(minutes);
    if (parsed !== null && (!Number.isFinite(parsed) || parsed <= 0)) return;
    update.mutate({ session_timeout_override: parsed as number | null });
  };

  return (
    <section className="rounded-lg border border-border bg-card">
      <header className="border-b border-border px-4 py-3">
        <h2 className="text-sm font-semibold">Session idle timeout</h2>
        <p className="text-muted-foreground text-xs mt-1">
          Override the global idle timeout for sessions in this org. Leave blank to use the system
          default. Members idle past this window are signed out on their next request.
        </p>
      </header>
      <div className="px-4 py-4">
        <div className="flex items-end gap-2">
          <div className="flex-1 flex flex-col gap-1.5">
            <Label htmlFor="session-timeout">Minutes (blank = default)</Label>
            <Input
              id="session-timeout"
              value={minutes}
              onChange={(e) => setMinutes(e.target.value)}
              placeholder="e.g. 480"
              data-testid="session-timeout-input"
              inputMode="numeric"
            />
          </div>
          <Button data-testid="session-timeout-save" disabled={update.isPending} onClick={onSave}>
            {update.isPending ? "Saving…" : "Save"}
          </Button>
        </div>
        {update.isError && (
          <p className="text-xs text-destructive mt-2" data-testid="session-timeout-err">
            {(update.error as Error)?.message || "Failed"}
          </p>
        )}
        {update.isSuccess && (
          <p className="text-xs text-emerald-600 mt-2" data-testid="session-timeout-ok">
            Saved.
          </p>
        )}
      </div>
    </section>
  );
}
