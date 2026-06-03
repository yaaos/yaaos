import { Button } from "@shared/components/ui/button";
import type { PluginMeta } from "./types";

/**
 * Reusable picker UI. Consumers (VCS + Coding Agents) supply a filtered
 * `plugins` list, an optional `installed` predicate for greying-out already-
 * installed entries, and an `onPick` click handler. Empty list renders an
 * informational placeholder rather than nothing so the caller doesn't have to.
 * Loading and error states are handled by the surrounding `<Suspense>` +
 * `<ErrorBoundary>` — callers never see an undefined `plugins`.
 */
interface Props {
  plugins: PluginMeta[];
  onPick: (plugin: PluginMeta) => void;
  /** Optional: returns true for plugins already installed (rendered as grey + disabled Add). */
  isInstalled?: (plugin: PluginMeta) => boolean;
  /** Test-time hook so multiple pickers can coexist on the same DOM. */
  testIdPrefix?: string;
}

export function PluginPicker({
  plugins,
  onPick,
  isInstalled,
  testIdPrefix = "plugin-picker",
}: Props) {
  if (plugins.length === 0) {
    return (
      <p className="text-muted-foreground p-4 text-sm" data-testid={`${testIdPrefix}-empty`}>
        No plugins available.
      </p>
    );
  }
  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2" data-testid={testIdPrefix}>
      {plugins.map((p) => {
        const installed = isInstalled?.(p) ?? false;
        return (
          <div
            key={p.id}
            className="rounded-lg border border-border bg-card px-4 py-3"
            data-testid={`${testIdPrefix}-card-${p.id}`}
          >
            <div className="flex items-start gap-3">
              <div className="flex-1">
                <h3 className="text-sm font-semibold">{p.display_name}</h3>
                {p.description && (
                  <p className="text-muted-foreground mt-1 text-xs">{p.description}</p>
                )}
                {p.docs_url && (
                  <a
                    href={p.docs_url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-primary mt-2 inline-block text-xs hover:underline"
                    data-testid={`${testIdPrefix}-docs-${p.id}`}
                  >
                    Docs ↗
                  </a>
                )}
              </div>
              <Button
                size="sm"
                data-testid={`${testIdPrefix}-add-${p.id}`}
                disabled={installed}
                onClick={() => onPick(p)}
              >
                {installed ? "Installed" : "Add"}
              </Button>
            </div>
          </div>
        );
      })}
    </div>
  );
}
