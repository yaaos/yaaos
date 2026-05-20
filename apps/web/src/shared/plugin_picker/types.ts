/** Mirror of the backend `PluginMeta` payload from `GET /api/plugins/available`. */
export interface PluginMeta {
  id: string;
  type: string;
  display_name: string;
  description: string | null;
  docs_url: string | null;
}

export interface ListAvailableResponse {
  plugins: PluginMeta[];
}
