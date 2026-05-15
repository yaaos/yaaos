import { useQuery } from "@tanstack/react-query";
import { type HealthResponse, apiClient } from "./client";

export function useHealth() {
  return useQuery<HealthResponse>({
    queryKey: ["health"],
    queryFn: async () => {
      const { data, error } = await apiClient.GET("/api/health");
      if (error) throw new Error("health check failed");
      if (!data) throw new Error("no data");
      return data;
    },
    refetchInterval: 5_000,
  });
}
