import { useHealth } from "@core/api";
import { Badge, Card, CardContent, CardHeader } from "@shared/components";

export function DashboardPage() {
  const { data, isLoading, isError, error } = useHealth();

  return (
    <div className="mx-auto max-w-[900px] flex flex-col gap-5">
      <div>
        <h1 className="text-[20px] font-semibold tracking-tight">Hello World</h1>
        <p className="text-text-3 text-[12.5px] mt-1">
          The skeleton is up. Below is the live response from{" "}
          <code className="mono bg-surface-2 px-1 py-0.5 rounded">GET /api/health</code>.
        </p>
      </div>

      <Card>
        <CardHeader>
          <h2 className="font-semibold text-[13.5px]">/api/health</h2>
          <div className="flex-1" />
          {data && (
            <Badge variant={data.db_ok ? "success" : "danger"}>
              {data.db_ok ? "healthy" : "degraded"}
            </Badge>
          )}
        </CardHeader>
        <CardContent>
          {isLoading && <div className="text-text-3 text-[12.5px]">Checking…</div>}
          {isError && (
            <div className="text-danger text-[12.5px]">
              Failed to reach /api/health: {(error as Error).message}
            </div>
          )}
          {data && (
            <div className="grid grid-cols-3 gap-4 text-[12.5px]">
              <Field label="status" value={data.status} />
              <Field label="db_ok" value={String(data.db_ok)} />
              <Field label="version" value={data.version} />
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="mono text-text-4 text-[10.5px] uppercase tracking-wider">{label}</span>
      <span className="mono text-text font-medium">{value}</span>
    </div>
  );
}
