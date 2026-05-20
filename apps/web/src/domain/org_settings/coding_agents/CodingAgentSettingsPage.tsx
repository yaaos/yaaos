import { Card, CardContent } from "@shared/components";
import { OrgSettingsLayout } from "../OrgSettingsLayout";
import { getPluginSettingsComponent } from "./plugin_registry";

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
          <Card>
            <CardContent>
              <h2 className="text-[16px] font-semibold mb-2">{pluginId}</h2>
              <p className="text-text-3 text-sm" data-testid="ca-settings-unavailable">
                Settings UI not available for this plugin.
              </p>
            </CardContent>
          </Card>
        )}
      </div>
    </OrgSettingsLayout>
  );
}
