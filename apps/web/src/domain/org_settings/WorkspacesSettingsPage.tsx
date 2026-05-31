/**
 * Org Settings > Workspaces.
 *
 * One workspace per org — a customer-deployed remote AWS agent.
 * The customer creates the IAM role in their AWS account, attaches it to
 * their agent compute (IRSA / instance profile / ECS task role), and pastes
 * the ARN + region here. yaaos never holds AWS credentials — the agent
 * sigv4-signs `GetCallerIdentity` with its own credentials and we replay
 * against AWS STS to verify.
 *
 * When the registered ARN changes or is cleared and running agents exist,
 * a confirmation dialog shows the affected agent count before saving.
 *
 * Org-admin only.
 */

import { getCurrentOrgSlug, useAgents } from "@core/api";
import { ConfirmModal } from "@shared/components/layout/confirm-modal";
import { Button } from "@shared/components/ui/button";
import { Input } from "@shared/components/ui/input";
import { Label } from "@shared/components/ui/label";
import { useEffect, useState } from "react";
import { OrgSettingsLayout } from "./OrgSettingsLayout";
import { useOrgSettings, useUpdateOrgSettings } from "./queries";

const BACKEND_URL = "https://app.yaaos.cloud";
const MIN_AGENT_VERSION_DISPLAY = "any";

// Narrow STS-enabled regions list. Mirrors the AWS STS endpoint allowlist
// the backend's `sts_verifier` accepts.
const AWS_REGIONS = [
  "us-east-1",
  "us-east-2",
  "us-west-1",
  "us-west-2",
  "eu-west-1",
  "eu-west-2",
  "eu-central-1",
  "ap-southeast-1",
  "ap-southeast-2",
  "ap-northeast-1",
];

const ARN_RE = /^arn:aws:iam::\d{12}:role\/[\w+=,.@-]+$/;

export function WorkspacesSettingsPage() {
  const { data, isLoading } = useOrgSettings();
  const update = useUpdateOrgSettings();
  const orgSlug = getCurrentOrgSlug() ?? "";
  const { data: agents } = useAgents(orgSlug);

  const [arn, setArn] = useState("");
  const [region, setRegion] = useState("us-east-1");
  const [confirmOpen, setConfirmOpen] = useState(false);

  useEffect(() => {
    if (!data) return;
    setArn(data.registered_iam_arn ?? "");
    setRegion(data.aws_region ?? "us-east-1");
  }, [data]);

  const arnValid = ARN_RE.test(arn);
  const canSave = arnValid && region.length > 0;

  // Count online + stale agents (those that would be disconnected on ARN change).
  const activeAgentCount = (agents ?? []).filter(
    (a) => a.state === "reachable" || a.state === "stale",
  ).length;

  // ARN is changing (or being cleared) when saved value differs from current.
  const arnChanging =
    data != null &&
    (arn.toLowerCase() !== (data.registered_iam_arn ?? "").toLowerCase() ||
      (data.registered_iam_arn != null && arn === ""));

  const onSave = () => {
    if (!canSave) return;
    // Show confirmation when the ARN is changing and running agents exist.
    if (arnChanging && activeAgentCount > 0) {
      setConfirmOpen(true);
      return;
    }
    doSave();
  };

  const doSave = () => {
    setConfirmOpen(false);
    update.mutate({
      registered_iam_arn: arn,
      aws_region: region,
    });
  };

  if (isLoading || !data) {
    return (
      <OrgSettingsLayout active="workspaces">
        <div className="mx-auto max-w-[900px] p-6 text-muted-foreground">Loading…</div>
      </OrgSettingsLayout>
    );
  }

  return (
    <OrgSettingsLayout active="workspaces">
      <div className="mx-auto flex max-w-[900px] flex-col gap-4 p-6">
        <RemoteConfigCard
          arn={arn}
          setArn={setArn}
          arnValid={arnValid}
          region={region}
          setRegion={setRegion}
          canSave={canSave}
          pending={update.isPending}
          onSave={onSave}
          error={update.isError ? String(update.error) : null}
        />
        <SetupChecklistCard region={region} />
      </div>

      <ConfirmModal
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title="Change registered ARN?"
        body={`This will disconnect ${activeAgentCount} running WorkspaceAgent${activeAgentCount === 1 ? "" : "s"} and fail their in-flight Workspaces. Continue?`}
        confirmLabel="Change ARN"
        tone="destructive"
        onConfirm={doSave}
        pending={update.isPending}
      />
    </OrgSettingsLayout>
  );
}

function RemoteConfigCard({
  arn,
  setArn,
  arnValid,
  region,
  setRegion,
  canSave,
  pending,
  onSave,
  error,
}: {
  arn: string;
  setArn: (v: string) => void;
  arnValid: boolean;
  region: string;
  setRegion: (v: string) => void;
  canSave: boolean;
  pending: boolean;
  onSave: () => void;
  error: string | null;
}) {
  return (
    <section className="rounded-lg border border-border bg-card">
      <header className="border-b border-border px-4 py-3">
        <h2 className="text-sm font-semibold">AWS configuration</h2>
        <p className="text-muted-foreground text-xs mt-1">
          Paste the IAM <strong>role</strong> ARN — not a session/assumed-role ARN. The verifier
          canonicalizes assumed-role ARNs server-side, but the registered value must be the role ARN
          itself.
        </p>
      </header>
      <div className="px-4 py-4 flex flex-col gap-3">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="iam-arn">IAM role ARN</Label>
          <Input
            id="iam-arn"
            data-testid="workspace-iam-arn"
            value={arn}
            onChange={(e) => setArn(e.target.value)}
            placeholder="arn:aws:iam::123456789012:role/yaaos-agent"
            aria-invalid={arn !== "" && !arnValid}
          />
          {arn !== "" && !arnValid && (
            <p className="text-destructive text-xs">
              Must match <code>arn:aws:iam::ACCOUNT:role/NAME</code>.
            </p>
          )}
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="aws-region">AWS region</Label>
          <select
            id="aws-region"
            data-testid="workspace-aws-region"
            className="rounded-md border border-border bg-background px-3 py-2 text-sm"
            value={region}
            onChange={(e) => setRegion(e.target.value)}
          >
            {AWS_REGIONS.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </div>
        <div className="flex items-center gap-2">
          <Button data-testid="workspace-save" disabled={!canSave || pending} onClick={onSave}>
            {pending ? "Saving…" : "Save"}
          </Button>
          {error && <span className="text-destructive text-xs">{error}</span>}
        </div>
      </div>
    </section>
  );
}

function SetupChecklistCard({ region }: { region: string }) {
  return (
    <section className="rounded-lg border border-border bg-card">
      <header className="border-b border-border px-4 py-3">
        <h2 className="text-sm font-semibold">Agent deployment</h2>
      </header>
      <div className="px-4 py-4 flex flex-col gap-2 text-sm">
        <p>
          <span className="text-muted-foreground">Backend URL: </span>
          <code>{BACKEND_URL}</code>
        </p>
        <p>
          <span className="text-muted-foreground">Minimum agent version: </span>
          <code>{MIN_AGENT_VERSION_DISPLAY}</code>
        </p>
        <p className="text-muted-foreground text-xs">
          Create an IAM role in your AWS account (no trust to yaaos required, no extra permissions
          needed). Attach it to your agent's compute (IRSA / EC2 instance profile / ECS task role).
          Your agent's VPC needs outbound HTTPS egress to <code>{BACKEND_URL}</code>. Air-gapped
          VPCs are not supported.
        </p>
        <pre
          className="bg-muted p-3 rounded text-xs overflow-x-auto"
          data-testid="deploy-snippet"
        >{`docker run --rm \\
  -e YAAOS_AGENT_POD_ID=<your-pod-uuid> \\
  -e AWS_REGION=${region} \\
  --user $(id -u) \\
  ghcr.io/yaaos/agent:latest supervisor`}</pre>
      </div>
    </section>
  );
}
