import { apiFetch } from "@core/api";
import { useQuery } from "@tanstack/react-query";
import type { ListAvailableResponse, PluginMeta } from "./types";

/** GET /api/plugins/available?type=... */
export function useAvailablePlugins(type: "vcs" | "coding_agent") {
  return useQuery<PluginMeta[]>({
    queryKey: ["plugins", "available", type],
    queryFn: async () => {
      const body = await apiFetch<ListAvailableResponse>(
        `/api/plugins/available?type=${encodeURIComponent(type)}`,
      );
      return body.plugins;
    },
    staleTime: 60_000,
  });
}
