/**
 * Login page.
 *
 * Two entry points:
 *   1. **Sign in with GitHub** at the top — always rendered when the `github`
 *      provider is configured. No email gate.
 *   2. **Email-first SSO discovery** below it — for enterprise customers
 *      whose org claims their domain to a SAML IdP. Hitting "Continue" calls
 *      `/api/auth/sso/discover`; on a SAML hit, render the SAML button.
 *
 * The test stub provider (`oauth_test`) surfaces in the same picker as
 * additional providers in dev/test only.
 */

import { useSsoDiscover } from "@core/api";
import { Button } from "@shared/components/ui/button";
import { Input } from "@shared/components/ui/input";
import { Skeleton } from "@shared/components/ui/skeleton";
import { Mail } from "lucide-react";
import { useState } from "react";
import { useProviders } from "./queries";

export function LoginPage() {
  const { data: providers, isLoading } = useProviders();
  const next = new URLSearchParams(window.location.search).get("next") ?? "/";
  const [email, setEmail] = useState("");
  const discover = useSsoDiscover();

  const onContinue = () => {
    const trimmed = email.trim();
    if (trimmed && /@/.test(trimmed)) {
      discover.mutate(trimmed);
    }
  };

  const startProvider = (id: string) => {
    const url = `/api/auth/login?provider=${encodeURIComponent(id)}&next=${encodeURIComponent(next)}`;
    // nosemgrep: javascript.browser.security.open-redirect.js-open-redirect
    window.location.href = url;
  };

  const samlResult = discover.data?.provider === "saml" ? discover.data : null;
  const githubAvailable = providers?.providers.includes("github") ?? false;
  const otherProviders = (providers?.providers ?? []).filter((p) => p !== "github");

  return (
    <div className="mx-auto max-w-[400px] mt-24 px-6">
      <div className="rounded-lg border border-border bg-card p-6 flex flex-col gap-4">
        <header>
          <h1 className="text-xl font-semibold tracking-tight">Sign in to yaaos</h1>
        </header>

        {isLoading && <Skeleton className="h-9" />}
        {!isLoading && githubAvailable && (
          <Button
            variant="default"
            onClick={() => startProvider("github")}
            data-testid="login-github"
          >
            Sign in with GitHub
          </Button>
        )}
        {!isLoading && !githubAvailable && otherProviders.length === 0 && (
          <p className="text-xs text-muted-foreground">No identity providers configured.</p>
        )}

        <div className="flex flex-col gap-2 border-t border-border pt-3">
          <span className="text-xs text-muted-foreground">SSO for enterprise</span>
          <form
            className="flex flex-col gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              onContinue();
            }}
          >
            <div className="relative">
              <Mail className="absolute left-2 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
              <Input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                data-testid="login-email"
                className="pl-8"
                autoComplete="email"
                required
              />
            </div>
            <Button
              type="submit"
              variant="outline"
              disabled={!email.trim() || discover.isPending}
              data-testid="login-continue"
            >
              {discover.isPending ? "Checking…" : "Continue"}
            </Button>
          </form>

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
      </div>
    </div>
  );
}
