// Side-effect imports: register plugin components in the registry before the
// first getPluginSettingsComponent call.
import "../../coding_agents/plugins/claude_code";
import "../../coding_agents/plugins/codex";
import { OrgSettingsLayout } from "@shared/components/public/layout/org-settings-layout";
import { getPluginSettingsComponent } from "../../coding_agents/plugin_registry";

/**
 * Per-plugin settings dispatcher. Renders the component registered for
 * `pluginId` in `plugin_registry`, or a placeholder when no component is
 * registered for it.
 */
export function CodingAgentSettingsPage({ pluginId }: { pluginId: string }) {
  const Component = getPluginSettingsComponent(pluginId);
  return (
    <OrgSettingsLayout active="coding-agents">
      <div className="mx-auto flex max-w-[900px] flex-col gap-4 p-6">
        {Component ? (
          <Component pluginId={pluginId} />
        ) : (
          <section className="rounded-lg border border-border bg-card px-4 py-4">
            <h2 className="text-base font-semibold mb-2">{pluginId}</h2>
            <p className="text-muted-foreground text-sm" data-testid="ca-settings-unavailable">
              Settings UI not available for this plugin.
            </p>
          </section>
        )}
      </div>
    </OrgSettingsLayout>
  );
}
