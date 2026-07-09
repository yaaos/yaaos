import { OrgSettingsLayout } from "@shared/components/public/layout/org-settings-layout";
import { MembersPage } from "../MembersPage";

export function MembersSettingsPage() {
  return (
    <OrgSettingsLayout active="members">
      <MembersPage />
    </OrgSettingsLayout>
  );
}
