import { registerPluginSettingsComponent } from "../../plugin_registry";
import { CodexSettings } from "./CodexSettings";

registerPluginSettingsComponent("codex", CodexSettings);

export { CodexSettings };
