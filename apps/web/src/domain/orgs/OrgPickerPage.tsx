/**
 * Org picker — sparse landing for multi-org users.
 *
 * Phase 2 ships a placeholder; Phase 8 implements the full E2a.19 card-grid
 * with role badge + last-used time + create-org modal.
 */

import { EmptyState, PageHeader } from "@shared/components/layout";
import { Building2 } from "lucide-react";

export function OrgPickerPage() {
  return (
    <div className="mx-auto max-w-[700px] px-6 py-8">
      <PageHeader title="Your organizations" subtitle="Pick one to keep working." />
      <EmptyState
        icon={Building2}
        headline="Org picker placeholder."
        body="The full picker (role badges, last-used time, create-org) lands in Phase 8 of M06."
      />
    </div>
  );
}
