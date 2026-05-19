import { apiFetch } from "@core/api";
import { Badge, Button, Card, CardContent, CardHeader } from "@shared/components";
import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { useCurrentUser, useLogoutAll } from "./queries";

/**
 * `/account` — user-scoped page (not org-scoped). Lists verified emails,
 * exposes the TOTP-setup entry point (Phase 11 lands the actual flow),
 * and the "Sign out of all sessions" action.
 */
export function AccountPage() {
  const { data, isLoading } = useCurrentUser();
  const logoutAll = useLogoutAll();

  if (isLoading) return <div className="p-6">Loading…</div>;
  if (!data) {
    return (
      <div className="p-6">
        Not signed in. <a href="/login">Go to login.</a>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-[900px] p-6 flex flex-col gap-4">
      <h1 className="text-[20px] font-semibold tracking-tight">Account</h1>

      <Card>
        <CardHeader>
          <h2 className="font-semibold text-[13.5px]">Profile</h2>
        </CardHeader>
        <CardContent>
          <div className="text-sm">
            <div className="font-medium">{data.user.display_name}</div>
            <div className="text-text-3 text-xs">{data.user.primary_email}</div>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <h2 className="font-semibold text-[13.5px]">Emails</h2>
        </CardHeader>
        <CardContent>
          <ul className="flex flex-col gap-2 text-sm">
            {data.user.emails.map((e) => (
              <li key={e.email} className="flex items-center gap-2">
                <span>{e.email}</span>
                {e.is_primary && <Badge variant="success">primary</Badge>}
                {e.verified ? (
                  <Badge variant="soft">verified</Badge>
                ) : (
                  <Badge variant="danger">unverified</Badge>
                )}
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>

      <TotpSetupCard />

      <Card>
        <CardHeader>
          <h2 className="font-semibold text-[13.5px]">Sessions</h2>
        </CardHeader>
        <CardContent>
          <p className="text-text-3 text-xs mb-2">
            Sign out of every browser this account has signed in from.
          </p>
          <Button
            onClick={() =>
              logoutAll.mutate(undefined, {
                onSuccess: () => {
                  window.location.href = "/login";
                },
              })
            }
            disabled={logoutAll.isPending}
            data-testid="logout-all"
          >
            {logoutAll.isPending ? "Signing out…" : "Sign out of all sessions"}
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}

function TotpSetupCard() {
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
    <Card>
      <CardHeader>
        <h2 className="font-semibold text-[13.5px]">Two-factor authentication</h2>
      </CardHeader>
      <CardContent>
        {!enrolled && (
          <Button
            onClick={() => enroll.mutate()}
            disabled={enroll.isPending}
            data-testid="totp-setup"
          >
            {enroll.isPending ? "Generating…" : "Set up 2FA"}
          </Button>
        )}
        {enrolled && !verified && (
          <div className="flex flex-col gap-2 text-sm">
            <p className="text-text-3 text-xs">
              Scan the QR or type the seed into your authenticator app, then enter a code.
            </p>
            <code className="mono text-xs break-all">{enrolled.otpauth_uri}</code>
            <code className="mono text-xs">{enrolled.seed}</code>
            <div className="flex gap-2">
              <input
                value={code}
                onChange={(e) => setCode(e.target.value)}
                placeholder="6-digit code"
                className="border rounded px-2 py-1 text-sm"
                data-testid="totp-code"
              />
              <Button
                onClick={() => verify.mutate(code)}
                disabled={!code || verify.isPending}
                data-testid="totp-verify"
              >
                {verify.isPending ? "Verifying…" : "Verify"}
              </Button>
            </div>
            {verify.isError && (
              <p className="text-red-500 text-xs">
                {(verify.error as Error)?.message ?? "Verify failed"}
              </p>
            )}
          </div>
        )}
        {verified && <Badge variant="success">2FA enabled</Badge>}
      </CardContent>
    </Card>
  );
}
