import { Button, Card, CardContent, CardHeader } from "@shared/components";
import { useState } from "react";
import { SsoConfigPage } from "../orgs/SsoConfigPage";
import { OrgSettingsLayout } from "./OrgSettingsLayout";
import { useUpdateOrgSettings } from "./queries";

/**
 * Org Settings > Auth: re-home of the M02 SSO config UI + a new
 * session-timeout override editor (Phase 4). Owner+Admin only.
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
  // Display starts blank; the existing value would come from /api/auth/me
  // (extended in Phase 6) — we'll wire it through once needed. For now the
  // form is write-only with explicit "Clear" semantics.
  const [minutes, setMinutes] = useState<string>("");
  const update = useUpdateOrgSettings();

  const onSave = () => {
    const parsed = minutes.trim() === "" ? null : Number(minutes);
    if (parsed !== null && (!Number.isFinite(parsed) || parsed <= 0)) return;
    update.mutate({ session_timeout_override: parsed as number | null });
  };

  return (
    <Card>
      <CardHeader>
        <h2 className="text-[13.5px] font-semibold">Session idle timeout</h2>
      </CardHeader>
      <CardContent>
        <p className="text-text-3 mb-2 text-xs">
          Override the global idle timeout for sessions in this org. Leave blank to use the system
          default. Members whose session is idle past this window are signed out on their next
          request.
        </p>
        <div className="flex items-center gap-2">
          <input
            value={minutes}
            onChange={(e) => setMinutes(e.target.value)}
            placeholder="minutes (blank = default)"
            data-testid="session-timeout-input"
            className="flex-1 rounded border border-border-soft bg-bg-2 px-2 py-1 text-sm"
            inputMode="numeric"
          />
          <Button data-testid="session-timeout-save" disabled={update.isPending} onClick={onSave}>
            {update.isPending ? "Saving…" : "Save"}
          </Button>
        </div>
        {update.isError && (
          <p className="text-xs text-red-500 mt-2" data-testid="session-timeout-err">
            {(update.error as Error)?.message || "Failed"}
          </p>
        )}
        {update.isSuccess && (
          <p className="text-xs text-success mt-2" data-testid="session-timeout-ok">
            Saved.
          </p>
        )}
      </CardContent>
    </Card>
  );
}
