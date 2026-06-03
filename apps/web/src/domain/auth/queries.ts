import { apiFetch } from "@core/api";
import { useSuspenseQuery } from "@tanstack/react-query";

export {
  type CurrentUser,
  type EmailSummary,
  type MembershipSummary,
  useCurrentUser,
  useLogout,
  useLogoutAll,
} from "@core/api";

export function useProviders() {
  return useSuspenseQuery<{ providers: string[] }>({
    queryKey: ["auth", "providers"],
    queryFn: () => apiFetch<{ providers: string[] }>("/api/auth/providers"),
    staleTime: 60_000,
  });
}
