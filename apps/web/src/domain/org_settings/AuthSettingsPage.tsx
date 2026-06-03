import { zodResolver } from "@hookform/resolvers/zod";
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
import { useForm } from "react-hook-form";
import { z } from "zod";
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

const sessionTimeoutSchema = z.object({
  minutes: z
    .string()
    .refine(
      (v) => v === "" || (Number.isFinite(Number(v)) && Number(v) > 0),
      "Enter a positive number or leave blank for the system default.",
    ),
});

type SessionTimeoutValues = z.infer<typeof sessionTimeoutSchema>;

function SessionTimeoutCard() {
  const update = useUpdateOrgSettings();

  const form = useForm<SessionTimeoutValues>({
    resolver: zodResolver(sessionTimeoutSchema),
    defaultValues: { minutes: "" },
  });

  const onSave = (values: SessionTimeoutValues) => {
    const parsed = values.minutes === "" ? null : Number(values.minutes);
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
        <Form {...form}>
          <form onSubmit={form.handleSubmit(onSave)}>
            <div className="flex items-end gap-2">
              <FormField
                control={form.control}
                name="minutes"
                render={({ field }) => (
                  <FormItem className="flex-1">
                    <FormLabel>Minutes (blank = default)</FormLabel>
                    <FormControl>
                      <Input
                        {...field}
                        id="session-timeout"
                        placeholder="e.g. 480"
                        data-testid="session-timeout-input"
                        inputMode="numeric"
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <Button type="submit" data-testid="session-timeout-save" disabled={update.isPending}>
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
          </form>
        </Form>
      </div>
    </section>
  );
}
