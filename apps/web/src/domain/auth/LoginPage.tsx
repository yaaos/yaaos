/**
 * Login page — E2a.18.
 *
 * Email-first flow: user types an email → "Continue" hits
 * `/api/auth/sso/discover` → render the right provider button
 * ("Continue with GitHub" or "Continue with [SAML IdP]"). The SSO branch
 * is github-only today (D8.1); the SPA contract is stable so once a
 * domain → org mapping lands the page lights up the SAML branch with
 * no further changes.
 *
 * The per-provider fallback buttons stay below for the test stub +
 * direct-GitHub sign-in (e2e specs depend on `data-testid="login-test"`).
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

  const result = discover.data;
  const showProviderPicker = result == null;

  return (
    <div className="mx-auto max-w-[400px] mt-24 px-6">
      <div className="rounded-lg border border-border bg-card p-6 flex flex-col gap-4">
        <header>
          <h1 className="text-xl font-semibold tracking-tight">Sign in to yaaos</h1>
          <p className="text-sm text-muted-foreground mt-1">Enter your work email to continue.</p>
        </header>

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
            disabled={!email.trim() || discover.isPending}
            data-testid="login-continue"
          >
            {discover.isPending ? "Checking…" : "Continue"}
          </Button>
        </form>

        {result?.provider === "github" && (
          <Button
            variant="default"
            onClick={() => startProvider("github")}
            data-testid="login-discovered-github"
          >
            Continue with GitHub
          </Button>
        )}
        {result?.provider === "saml" && (
          <Button
            variant="default"
            onClick={() => result.saml_org_slug && startProvider(`saml/${result.saml_org_slug}`)}
            data-testid="login-discovered-saml"
          >
            Continue with {result.saml_idp_name || "your SSO provider"}
          </Button>
        )}

        {showProviderPicker && (
          <div className="flex flex-col gap-2 border-t border-border pt-3">
            <span className="text-xs text-muted-foreground">Or sign in with</span>
            {isLoading && <Skeleton className="h-9" />}
            {providers && providers.providers.length === 0 && (
              <p className="text-xs text-muted-foreground">No identity providers configured.</p>
            )}
            {providers?.providers.map((p) => (
              <Button
                key={p}
                variant="outline"
                onClick={() => startProvider(p)}
                data-testid={`login-${p}`}
              >
                {p === "github" ? "GitHub" : p === "test" ? "test stub" : p}
              </Button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
