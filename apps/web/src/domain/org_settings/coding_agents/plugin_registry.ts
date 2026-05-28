import type { ComponentType } from "react";

/**
 * Per-plugin settings UI registry.
 *
 * Each first-party coding-agent plugin registers a bespoke React component
 * that renders its settings page. The per-plugin route
 * (`/orgs/$slug/settings/coding-agents/$pluginId`) looks up the component
 * here at navigation time. Plugins without a registered component fall back
 * to a "settings not available" placeholder.
 *
 * `claude_code` is the only registered plugin.
 */
export interface PluginSettingsComponentProps {
  pluginId: string;
}

export type PluginSettingsComponent = ComponentType<PluginSettingsComponentProps>;

const REGISTRY: Record<string, PluginSettingsComponent> = {};

export function registerPluginSettingsComponent(
  pluginId: string,
  component: PluginSettingsComponent,
): void {
  REGISTRY[pluginId] = component;
}

export function getPluginSettingsComponent(pluginId: string): PluginSettingsComponent | undefined {
  return REGISTRY[pluginId];
}

export function registeredPluginIds(): string[] {
  return Object.keys(REGISTRY).sort();
}

/** Test hook — never used in production code paths. */
export function _resetRegistryForTests(): void {
  for (const k of Object.keys(REGISTRY)) delete REGISTRY[k];
}
