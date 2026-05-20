import { Button, Card, CardContent } from "@shared/components";
import type { PluginMeta } from "./types";

/**
 * Reusable picker UI. Consumers (VCS + Coding Agents) supply a filtered
 * `plugins` list, an optional `installed` predicate for greying-out already-
 * installed entries, and an `onPick` click handler. Empty list renders an
 * informational placeholder rather than nothing so the caller doesn't have to.
 */
interface Props {
  plugins: PluginMeta[];
  onPick: (plugin: PluginMeta) => void;
  /** Optional: returns true for plugins already installed (rendered as grey + disabled Add). */
  isInstalled?: (plugin: PluginMeta) => boolean;
  loading?: boolean;
  error?: Error | null;
  /** Test-time hook so multiple pickers can coexist on the same DOM. */
  testIdPrefix?: string;
}

export function PluginPicker({
  plugins,
  onPick,
  isInstalled,
  loading,
  error,
  testIdPrefix = "plugin-picker",
}: Props) {
  if (loading) {
    return (
      <p className="text-text-3 p-4 text-sm" data-testid={`${testIdPrefix}-loading`}>
        Loading available plugins…
      </p>
    );
  }
  if (error) {
    return (
      <p className="p-4 text-sm text-red-500" data-testid={`${testIdPrefix}-error`}>
        Failed to load plugins: {error.message}
      </p>
    );
  }
  if (plugins.length === 0) {
    return (
      <p className="text-text-3 p-4 text-sm" data-testid={`${testIdPrefix}-empty`}>
        No plugins available.
      </p>
    );
  }
  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2" data-testid={testIdPrefix}>
      {plugins.map((p) => {
        const installed = isInstalled?.(p) ?? false;
        return (
          <Card key={p.id} data-testid={`${testIdPrefix}-card-${p.id}`}>
            <CardContent>
              <div className="flex items-start gap-3">
                <div className="flex-1">
                  <h3 className="text-[13.5px] font-semibold">{p.display_name}</h3>
                  {p.description && <p className="text-text-3 mt-1 text-xs">{p.description}</p>}
                  {p.docs_url && (
                    <a
                      href={p.docs_url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-accent mt-2 inline-block text-xs hover:underline"
                      data-testid={`${testIdPrefix}-docs-${p.id}`}
                    >
                      Docs ↗
                    </a>
                  )}
                </div>
                <Button
                  data-testid={`${testIdPrefix}-add-${p.id}`}
                  disabled={installed}
                  onClick={() => onPick(p)}
                >
                  {installed ? "Installed" : "Add"}
                </Button>
              </div>
            </CardContent>
          </Card>
        );
      })}
    </div>
  );
}
