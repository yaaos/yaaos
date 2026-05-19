import { Button, Card, CardContent, CardHeader } from "@shared/components";
import { useProviders } from "./queries";

/**
 * Provider-button login page. The button hits `GET /api/auth/login?provider=<id>`
 * which 302's to the IdP; on callback, the SPA lands at `next` and
 * `useCurrentUser` flips to authenticated.
 *
 * The `test` provider only appears when `YAAOS_ENV=test` (the backend
 * doesn't register it otherwise). Playwright drives login via that button.
 */
export function LoginPage() {
  const { data, isLoading } = useProviders();
  const next = new URLSearchParams(window.location.search).get("next") ?? "/";
  return (
    <div className="mx-auto max-w-[400px] mt-24 p-6">
      <Card>
        <CardHeader>
          <h1 className="text-[18px] font-semibold tracking-tight">Sign in to yaaos</h1>
        </CardHeader>
        <CardContent>
          {isLoading && <p className="text-text-3 text-xs">Loading…</p>}
          {data && data.providers.length === 0 && (
            <p className="text-text-3 text-xs">No identity providers configured.</p>
          )}
          {data && (
            <div className="flex flex-col gap-2">
              {data.providers.map((p) => (
                <Button
                  key={p}
                  onClick={() => {
                    const url = `/api/auth/login?provider=${encodeURIComponent(p)}&next=${encodeURIComponent(next)}`;
                    window.location.href = url;
                  }}
                  data-testid={`login-${p}`}
                >
                  Sign in with {p === "github" ? "GitHub" : p === "test" ? "test stub" : p}
                </Button>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
