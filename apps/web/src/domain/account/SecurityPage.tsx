import { apiFetch } from "@core/api";
import { useLogoutAll } from "@domain/auth";
import { PageHeader } from "@shared/components/layout";
import { Badge } from "@shared/components/ui/badge";
import { Button } from "@shared/components/ui/button";
import { Input } from "@shared/components/ui/input";
import { Label } from "@shared/components/ui/label";
import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

/**
 * `/user/security` — re-homed TOTP enrollment + sign-out-all-sessions from
 * the M02 `/account` page. Future security settings (recovery codes, passkeys,
 * hardware keys) land here.
 */
export function SecurityPage() {
  const logoutAll = useLogoutAll();
  return (
    <div className="mx-auto flex max-w-[900px] flex-col gap-6 p-6">
      <PageHeader title="Security" subtitle="Two-factor authentication and active sessions." />
      <TotpSection />
      <Section
        title="Sessions"
        description="Sign out of every browser this account has signed in from."
      >
        <Button
          variant="destructive"
          data-testid="logout-all"
          disabled={logoutAll.isPending}
          onClick={() =>
            logoutAll.mutate(undefined, {
              onSuccess: () => {
                window.location.href = "/login";
              },
            })
          }
        >
          {logoutAll.isPending ? "Signing out…" : "Sign out of all sessions"}
        </Button>
      </Section>
    </div>
  );
}

function Section({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-lg border border-border bg-card">
      <header className="border-b border-border px-4 py-3">
        <h2 className="text-sm font-semibold">{title}</h2>
        {description && <p className="text-muted-foreground text-xs mt-1">{description}</p>}
      </header>
      <div className="px-4 py-4">{children}</div>
    </section>
  );
}

function TotpSection() {
  const [enrolled, setEnrolled] = useState<{ seed: string; otpauth_uri: string } | null>(null);
  const [code, setCode] = useState("");
  const [verified, setVerified] = useState(false);

  const enroll = useMutation({
    mutationFn: () =>
      apiFetch<{ seed: string; otpauth_uri: string }>("/api/auth/totp/enroll", { method: "POST" }),
    onSuccess: (data) => setEnrolled(data),
  });

  const verify = useMutation({
    mutationFn: (c: string) =>
      apiFetch("/api/auth/totp/verify", {
        method: "POST",
        body: JSON.stringify({ code: c }),
      }),
    onSuccess: () => setVerified(true),
  });

  return (
    <Section
      title="Two-factor authentication"
      description="Add a 6-digit code from your authenticator app on every sign-in."
    >
      {!enrolled && (
        <Button
          data-testid="totp-setup"
          disabled={enroll.isPending}
          onClick={() => enroll.mutate()}
        >
          {enroll.isPending ? "Generating…" : "Set up 2FA"}
        </Button>
      )}
      {enrolled && !verified && (
        <div className="flex flex-col gap-3 text-sm">
          <p className="text-muted-foreground text-xs">
            Scan the QR or type the seed into your authenticator app, then enter a code.
          </p>
          <code className="break-all rounded bg-muted px-2 py-1.5 font-mono text-xs">
            {enrolled.otpauth_uri}
          </code>
          <code className="rounded bg-muted px-2 py-1.5 font-mono text-xs">{enrolled.seed}</code>
          <div className="flex items-end gap-2">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="totp-code">6-digit code</Label>
              <Input
                id="totp-code"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                placeholder="000000"
                inputMode="numeric"
                maxLength={6}
                className="w-32"
                data-testid="totp-code"
              />
            </div>
            <Button
              data-testid="totp-verify"
              disabled={!code || verify.isPending}
              onClick={() => verify.mutate(code)}
            >
              {verify.isPending ? "Verifying…" : "Verify"}
            </Button>
          </div>
          {verify.isError && (
            <p className="text-xs text-destructive">
              {(verify.error as Error)?.message ?? "Verify failed"}
            </p>
          )}
        </div>
      )}
      {verified && <Badge>2FA enabled</Badge>}
    </Section>
  );
}
