import { zodResolver } from "@hookform/resolvers/zod";
import { ErrorBanner } from "@shared/components/public/layout/error-banner";
import { Badge } from "@shared/components/ui/badge";
import { Button } from "@shared/components/ui/button";
import { Form, FormControl, FormField, FormItem, FormMessage } from "@shared/components/ui/form";
import { Input } from "@shared/components/ui/input";
import { Suspense, useEffect, useState } from "react";
import { ErrorBoundary } from "react-error-boundary";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { type CodingAgentInstall, useCodingAgents } from "../../queries";
import {
  useClearOpenAIKey,
  useOpenAIKeyStatus,
  useSetOpenAIKey,
  useValidateOpenAIKey,
} from "./queries";

/**
 * Bespoke settings UI for the `codex` coding-agent plugin.
 *
 * Renders:
 *  - OpenAI API key card (provider=openai — test/save/rotate/clear).
 */
export function CodexSettings({ pluginId }: { pluginId: string }) {
  return (
    <ErrorBoundary
      fallbackRender={({ resetErrorBoundary }) => (
        <ErrorBanner message="Couldn't load Codex settings." onRetry={resetErrorBoundary} />
      )}
    >
      <Suspense fallback={<p className="text-muted-foreground p-4 text-sm">Loading…</p>}>
        <CodexContent pluginId={pluginId} />
      </Suspense>
    </ErrorBoundary>
  );
}

function CodexContent({ pluginId }: { pluginId: string }) {
  const { data: installs } = useCodingAgents();

  const install = installs.find((i: CodingAgentInstall) => i.plugin_id === pluginId);

  if (!install) {
    return (
      <section className="rounded-lg border border-border bg-card">
        <div className="px-4 py-4">
          <p className="text-muted-foreground text-sm" data-testid="codex-not-installed">
            Codex is not installed for this org. Install it from the Coding Agents page first.
          </p>
        </div>
      </section>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <OpenAIKeyCard />
    </div>
  );
}

// ── OpenAI key card ───────────────────────────────────────────────────────────

const openAIKeySchema = z.object({
  value: z.string().min(1, "API key is required."),
});

type OpenAIKeyValues = z.infer<typeof openAIKeySchema>;

function OpenAIKeyCard() {
  const status = useOpenAIKeyStatus();
  const setKey = useSetOpenAIKey();
  const validate = useValidateOpenAIKey();
  const clear = useClearOpenAIKey();
  const configured = status.data?.status === "configured";
  const [editing, setEditing] = useState(!configured);

  useEffect(() => {
    if (status.data && configured) setEditing(false);
  }, [status.data, configured]);

  const keyForm = useForm<OpenAIKeyValues>({
    resolver: zodResolver(openAIKeySchema),
    defaultValues: { value: "" },
  });

  const onSaveKey = (values: OpenAIKeyValues) => {
    setKey.mutate(values.value, {
      onSuccess: () => {
        keyForm.reset();
        setEditing(false);
      },
    });
  };

  return (
    <section className="rounded-lg border border-border bg-card">
      <header className="border-b border-border px-4 py-3">
        <div className="flex items-center gap-2">
          <h3 className="text-[13.5px] font-semibold">OpenAI API key</h3>
          {configured ? (
            <Badge variant="default" data-testid="codex-key-configured">
              configured
            </Badge>
          ) : (
            <Badge variant="destructive" data-testid="codex-key-not-set">
              not set
            </Badge>
          )}
        </div>
      </header>
      <div className="px-4 py-4">
        {!editing && configured && (
          <div className="flex items-center gap-2">
            <span className="text-sm text-muted-foreground" data-testid="codex-key-summary">
              Configured ✓ · last set{" "}
              {status.data?.updated_at ? new Date(status.data.updated_at).toLocaleString() : "—"}
            </span>
            <Button
              type="button"
              data-testid="codex-key-test"
              disabled={validate.isPending}
              onClick={() => validate.mutate()}
            >
              {validate.isPending ? "Testing…" : "Test"}
            </Button>
            <Button type="button" data-testid="codex-key-rotate" onClick={() => setEditing(true)}>
              Rotate
            </Button>
            <Button
              type="button"
              data-testid="codex-key-clear"
              disabled={clear.isPending}
              onClick={() => clear.mutate()}
            >
              Clear
            </Button>
          </div>
        )}
        {editing && (
          <Form {...keyForm}>
            <form onSubmit={keyForm.handleSubmit(onSaveKey)} className="flex items-start gap-2">
              <FormField
                control={keyForm.control}
                name="value"
                render={({ field }) => (
                  <FormItem className="flex-1">
                    <FormControl>
                      <Input
                        {...field}
                        type="password"
                        placeholder={configured ? "Paste new API key to replace" : "sk-..."}
                        data-testid="codex-key-input"
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <Button type="submit" data-testid="codex-key-save" disabled={setKey.isPending}>
                {setKey.isPending ? "Saving…" : "Save"}
              </Button>
              {configured && (
                <Button
                  type="button"
                  data-testid="codex-key-rotate-cancel"
                  onClick={() => {
                    keyForm.reset();
                    setEditing(false);
                  }}
                >
                  Cancel
                </Button>
              )}
            </form>
          </Form>
        )}
        {validate.data && (
          <p
            className={`mt-2 text-xs ${validate.data.valid ? "text-emerald-600" : "text-destructive"}`}
            data-testid="codex-key-test-result"
          >
            {validate.data.valid ? "Key looks good." : "Key rejected."}
          </p>
        )}
      </div>
    </section>
  );
}
