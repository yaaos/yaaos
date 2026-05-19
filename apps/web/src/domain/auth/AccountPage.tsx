import { Badge, Button, Card, CardContent, CardHeader } from "@shared/components";
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

      <Card>
        <CardHeader>
          <h2 className="font-semibold text-[13.5px]">Two-factor authentication</h2>
        </CardHeader>
        <CardContent>
          <p className="text-text-3 text-xs">
            TOTP setup ships with Phase 11. Once landed, generate a QR code here.
          </p>
          <Button disabled data-testid="totp-setup">
            Set up 2FA (Phase 11)
          </Button>
        </CardContent>
      </Card>

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
