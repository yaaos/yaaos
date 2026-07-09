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

import { getCurrentOrgSlug } from "@core/api/public/org-context";
import { useAgents } from "@core/api/public/queries";
import { zodResolver } from "@hookform/resolvers/zod";
import { ConfirmModal } from "@shared/components/public/layout/confirm-modal";
import { ErrorBanner } from "@shared/components/public/layout/error-banner";
import { OrgSettingsLayout } from "@shared/components/public/layout/org-settings-layout";
import { Button } from "@shared/components/ui/button";
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@shared/components/ui/form";
import { Input } from "@shared/components/ui/input";
import { Skeleton } from "@shared/components/ui/skeleton";
import { useEffect, useState } from "react";
import { Suspense } from "react";
import { ErrorBoundary } from "react-error-boundary";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { useOrgSettings, useUpdateOrgSettings } from "../queries";

const BACKEND_URL = "https://app.yaaos.dev";
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

const workspacesSchema = z.object({
  arn: z
    .string()
    .min(1, "IAM role ARN is required.")
    .regex(ARN_RE, "Must match arn:aws:iam::ACCOUNT:role/NAME."),
  region: z.string().min(1, "Region is required."),
});

type WorkspacesValues = z.infer<typeof workspacesSchema>;

const limitsSchema = z.object({
  workspace_max_count: z
    .number({ invalid_type_error: "Must be a number." })
    .int("Whole number only.")
    .min(1, "Must be at least 1.")
    .max(50, "Max 50."),
});

type LimitsValues = z.infer<typeof limitsSchema>;

export function WorkspacesSettingsPage() {
  return (
    <OrgSettingsLayout active="workspaces">
      <ErrorBoundary
        fallbackRender={({ resetErrorBoundary }) => (
          <ErrorBanner message="Couldn't load workspace settings." onRetry={resetErrorBoundary} />
        )}
      >
        <Suspense
          fallback={
            <div className="mx-auto max-w-[900px] p-6">
              <Skeleton className="h-40 mb-4" />
              <Skeleton className="h-32" />
            </div>
          }
        >
          <WorkspacesContent />
        </Suspense>
      </ErrorBoundary>
    </OrgSettingsLayout>
  );
}

function WorkspacesContent() {
  const { data } = useOrgSettings();
  const update = useUpdateOrgSettings();
  const orgSlug = getCurrentOrgSlug() ?? "";
  const { data: agents } = useAgents(orgSlug);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [pendingValues, setPendingValues] = useState<WorkspacesValues | null>(null);

  const form = useForm<WorkspacesValues>({
    resolver: zodResolver(workspacesSchema),
    defaultValues: { arn: "", region: "us-east-1" },
  });

  useEffect(() => {
    if (!data) return;
    form.reset({
      arn: data.registered_iam_arn ?? "",
      region: data.aws_region ?? "us-east-1",
    });
  }, [data, form]);

  // Count online + stale agents (those that would be disconnected on ARN change).
  const activeAgentCount = (agents ?? []).filter(
    (a) => a.state === "reachable" || a.state === "stale",
  ).length;

  const arnChanging = (nextArn: string) =>
    data != null &&
    (nextArn.toLowerCase() !== (data.registered_iam_arn ?? "").toLowerCase() ||
      (data.registered_iam_arn != null && nextArn === ""));

  const doSave = (values: WorkspacesValues) => {
    setConfirmOpen(false);
    update.mutate({ registered_iam_arn: values.arn, aws_region: values.region });
  };

  const onSubmit = (values: WorkspacesValues) => {
    if (arnChanging(values.arn) && activeAgentCount > 0) {
      setPendingValues(values);
      setConfirmOpen(true);
      return;
    }
    doSave(values);
  };

  return (
    <div className="mx-auto flex max-w-[900px] flex-col gap-4 p-6">
      <section className="rounded-lg border border-border bg-card">
        <header className="border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold">AWS configuration</h2>
          <p className="text-muted-foreground text-xs mt-1">
            Paste the IAM <strong>role</strong> ARN — not a session/assumed-role ARN. The verifier
            canonicalizes assumed-role ARNs server-side, but the registered value must be the role
            ARN itself.
          </p>
        </header>
        <div className="px-4 py-4 flex flex-col gap-3">
          <Form {...form}>
            <form onSubmit={form.handleSubmit(onSubmit)} className="flex flex-col gap-3">
              <FormField
                control={form.control}
                name="arn"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>IAM role ARN</FormLabel>
                    <FormControl>
                      <Input
                        {...field}
                        id="iam-arn"
                        data-testid="workspace-iam-arn"
                        placeholder="arn:aws:iam::123456789012:role/yaaos-agent"
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="region"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>AWS region</FormLabel>
                    <FormControl>
                      <select
                        {...field}
                        id="aws-region"
                        data-testid="workspace-aws-region"
                        className="rounded-md border border-border bg-background px-3 py-2 text-sm w-full"
                      >
                        {AWS_REGIONS.map((r) => (
                          <option key={r} value={r}>
                            {r}
                          </option>
                        ))}
                      </select>
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <div className="flex items-center gap-2">
                <Button type="submit" data-testid="workspace-save" disabled={update.isPending}>
                  {update.isPending ? "Saving…" : "Save"}
                </Button>
                {update.isError && (
                  <span className="text-destructive text-xs">{String(update.error)}</span>
                )}
              </div>
            </form>
          </Form>
        </div>
      </section>

      <LimitsCard />

      <SetupChecklistCard region={form.watch("region")} />

      <ConfirmModal
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title="Change registered ARN?"
        body={`This will disconnect ${activeAgentCount} running WorkspaceAgent${activeAgentCount === 1 ? "" : "s"} and fail their in-flight Workspaces. Continue?`}
        confirmLabel="Change ARN"
        tone="destructive"
        onConfirm={() => pendingValues && doSave(pendingValues)}
        pending={update.isPending}
      />
    </div>
  );
}

function LimitsCard() {
  const { data } = useOrgSettings();
  const update = useUpdateOrgSettings();

  const form = useForm<LimitsValues>({
    resolver: zodResolver(limitsSchema),
    defaultValues: { workspace_max_count: data?.workspace_max_count ?? 4 },
  });

  useEffect(() => {
    if (!data) return;
    form.reset({ workspace_max_count: data.workspace_max_count });
  }, [data, form]);

  const onSubmit = (values: LimitsValues) => {
    update.mutate({ workspace_max_count: values.workspace_max_count });
  };

  return (
    <section className="rounded-lg border border-border bg-card">
      <header className="border-b border-border px-4 py-3">
        <h2 className="text-sm font-semibold">Limits</h2>
        <p className="text-muted-foreground text-xs mt-1">
          Cap on concurrent workspaces per WorkspaceAgent. Applies to every agent in the org on
          their next claim — no agent restart needed.
        </p>
      </header>
      <div className="px-4 py-4 flex flex-col gap-3">
        <Form {...form}>
          <form onSubmit={form.handleSubmit(onSubmit)} className="flex flex-col gap-3">
            <FormField
              control={form.control}
              name="workspace_max_count"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Max workspaces per agent</FormLabel>
                  <FormControl>
                    <Input
                      type="number"
                      min={1}
                      max={50}
                      step={1}
                      data-testid="workspace-max-count"
                      value={field.value}
                      onChange={(e) => field.onChange(e.target.valueAsNumber)}
                      onBlur={field.onBlur}
                      name={field.name}
                      ref={field.ref}
                      className="max-w-[120px]"
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <div className="flex items-center gap-2">
              <Button
                type="submit"
                data-testid="workspace-max-count-save"
                disabled={update.isPending}
              >
                {update.isPending ? "Saving…" : "Save"}
              </Button>
              {update.isError && (
                <span className="text-destructive text-xs">{String(update.error)}</span>
              )}
            </div>
          </form>
        </Form>
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
  -e AWS_REGION=${region} \\
  --user $(id -u) \\
  ghcr.io/yaaos/agent:latest supervisor`}</pre>
      </div>
    </section>
  );
}
