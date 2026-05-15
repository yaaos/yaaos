import { Card, CardContent, CardHeader } from "./card";

export function PlaceholderPage({ title }: { title: string }) {
  return (
    <div className="mx-auto max-w-[900px]">
      <Card>
        <CardHeader>
          <h2 className="font-semibold text-[13.5px]">{title}</h2>
        </CardHeader>
        <CardContent>
          <p className="text-text-3 text-[12.5px]">
            Coming in M01. This route exists so the sidebar nav is functional; the page itself is
            built as part of the M01 milestone.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
