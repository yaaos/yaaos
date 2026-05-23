/**
 * User — Messaging.
 *
 * Placeholder per E2a.17. The Messaging feature (Slack/Telegram/Email
 * destination opt-ins) is deferred to a future milestone; M06 ships the
 * route in place so the User popover's "Messaging" link doesn't 404.
 */

import { EmptyState, PageHeader } from "@shared/components/layout";
import { MessageSquare } from "lucide-react";

export function MessagingPage() {
  return (
    <div className="mx-auto max-w-[700px] px-6 py-8">
      <PageHeader title="Messaging" subtitle="Where yaaos pings you outside the app." />
      <EmptyState
        icon={MessageSquare}
        headline="Messaging is coming soon."
        body="In a future milestone you'll be able to opt in to Slack DMs, Telegram, or email digests. Today, all updates land in Notifications."
      />
    </div>
  );
}
