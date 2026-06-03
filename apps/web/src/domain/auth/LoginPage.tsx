/**
 * Login page.
 *
 * Two entry points:
 *   1. **Sign in with GitHub** at the top — always rendered when the `github`
 *      provider is configured. No email gate.
 *   2. **Email-first SSO discovery** below it — for enterprise customers
 *      whose org claims their domain to a SAML IdP. Hitting "Continue" calls
 *      `/api/sso/discover`; on a SAML hit, render the SAML button.
 *
 * The test stub provider (`oauth_test`) surfaces in the same picker as
 * additional providers in dev/test only.
 */

import { type AuthFailureReason, useSsoDiscover } from "@core/api";
import { zodResolver } from "@hookform/resolvers/zod";
import { ErrorBanner } from "@shared/components/layout";
import { Button } from "@shared/components/ui/button";
import { Form, FormControl, FormField, FormItem, FormMessage } from "@shared/components/ui/form";
import { Input } from "@shared/components/ui/input";
import { Skeleton } from "@shared/components/ui/skeleton";
import { Mail } from "lucide-react";
import { Suspense } from "react";
import { ErrorBoundary } from "react-error-boundary";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { useProviders } from "./queries";

/** Map the `?reason=` query param the central 401 handler sets to the
 * banner copy the user sees. Anything unrecognized renders no banner —
 * the bare /login page already explains itself. */
const REASON_COPY: Record<AuthFailureReason, string> = {
  idle: "Your session timed out from inactivity. Sign in to continue.",
  expired: "Your session expired. Sign in to continue.",
  signed_out: "You were signed out. Sign in to continue.",
  not_provisioned:
    "Your account doesn't exist in yaaos yet. Ask an admin to invite your email, then sign in again.",
};

function reasonFromQuery(search: string): AuthFailureReason | null {
  const v = new URLSearchParams(search).get("reason");
  if (v === "idle" || v === "expired" || v === "signed_out" || v === "not_provisioned") return v;
  return null;
}

const ssoEmailSchema = z.object({
  email: z.string().email("Enter a valid email address"),
});

type SsoEmailValues = z.infer<typeof ssoEmailSchema>;

export function LoginPage() {
  // `next` is the path the central 401 handler captured before redirecting,
  // OR a fresh deeplink the user pasted. Forwarded to the OAuth provider
  // via `?next=`; backend `_safe_next` validator rejects anything that
  // isn't a same-origin relative path.
  const next = new URLSearchParams(window.location.search).get("next") ?? "/";
  const reason = reasonFromQuery(window.location.search);

  return (
    <div className="mx-auto max-w-[400px] mt-24 px-6">
      <div className="rounded-lg border border-border bg-card p-6 flex flex-col gap-4">
        <header>
          <h1 className="text-xl font-semibold tracking-tight">Sign in to yaaos</h1>
        </header>

        {reason && (
          <output
            className="block rounded border border-amber-400/40 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-300/30 dark:bg-amber-950/40 dark:text-amber-100"
            data-testid="login-reason-banner"
          >
            {REASON_COPY[reason]}
          </output>
        )}

        <ErrorBoundary
          fallbackRender={({ resetErrorBoundary }) => (
            <ErrorBanner message="Couldn't load sign-in options." onRetry={resetErrorBoundary} />
          )}
        >
          <Suspense fallback={<Skeleton className="h-9" />}>
            <ProvidersSection next={next} />
          </Suspense>
        </ErrorBoundary>
      </div>
    </div>
  );
}

function ProvidersSection({ next }: { next: string }) {
  const { data: providers } = useProviders();
  const discover = useSsoDiscover();

  const form = useForm<SsoEmailValues>({
    resolver: zodResolver(ssoEmailSchema),
    defaultValues: { email: "" },
  });

  const onContinue = (values: SsoEmailValues) => {
    discover.mutate(values.email);
  };

  const startProvider = (id: string) => {
    const url = `/api/auth/login?provider=${encodeURIComponent(id)}&next=${encodeURIComponent(next)}`;
    // nosemgrep: javascript.browser.security.open-redirect.js-open-redirect
    window.location.href = url;
  };

  const samlResult = discover.data?.provider === "saml" ? discover.data : null;
  const githubAvailable = providers.providers.includes("github");
  const otherProviders = providers.providers.filter((p) => p !== "github");

  return (
    <>
      {githubAvailable && (
        <Button
          variant="default"
          onClick={() => startProvider("github")}
          data-testid="login-github"
        >
          Sign in with GitHub
        </Button>
      )}
      {!githubAvailable && otherProviders.length === 0 && (
        <p className="text-xs text-muted-foreground">No identity providers configured.</p>
      )}

      <div className="flex flex-col gap-2 border-t border-border pt-3">
        <span className="text-xs text-muted-foreground">SSO for enterprise</span>
        <Form {...form}>
          <form className="flex flex-col gap-2" onSubmit={form.handleSubmit(onContinue)}>
            <FormField
              control={form.control}
              name="email"
              render={({ field }) => (
                <FormItem>
                  <FormControl>
                    <div className="relative">
                      <Mail className="absolute left-2 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                      <Input
                        {...field}
                        type="email"
                        placeholder="you@example.com"
                        data-testid="login-email"
                        className="pl-8"
                        autoComplete="email"
                      />
                    </div>
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <Button
              type="submit"
              variant="outline"
              disabled={discover.isPending}
              data-testid="login-continue"
            >
              {discover.isPending ? "Checking…" : "Continue"}
            </Button>
          </form>
        </Form>

        {samlResult && (
          <Button
            variant="default"
            onClick={() =>
              samlResult.saml_org_slug && startProvider(`saml/${samlResult.saml_org_slug}`)
            }
            data-testid="login-discovered-saml"
          >
            Continue with {samlResult.saml_idp_name || "your SSO provider"}
          </Button>
        )}
      </div>

      {otherProviders.length > 0 && (
        <div className="flex flex-col gap-2 border-t border-border pt-3">
          <span className="text-xs text-muted-foreground">Other</span>
          {otherProviders.map((p) => (
            <Button
              key={p}
              variant="outline"
              onClick={() => startProvider(p)}
              data-testid={`login-${p}`}
            >
              {p === "test" ? "test stub" : p}
            </Button>
          ))}
        </div>
      )}
    </>
  );
}
