/**
 * Notifications — full page placeholder.
 *
 * Phase 2 ships the route + shell; Phase 7 wires the real
 * `apps/backend/app/domain/notifications` module + SSE live updates.
 */

import { EmptyState, PageHeader } from "@shared/components/layout";
import { Bell } from "lucide-react";

export function NotificationsPage() {
  return (
    <div className="mx-auto max-w-[900px] px-6 py-8">
      <PageHeader title="Notifications" subtitle="Cross-org inbox." />
      <EmptyState
        icon={Bell}
        headline="No notifications yet."
        body="When yaaos needs a decision on one of your tickets, or finishes a review, it shows up here."
      />
    </div>
  );
}
