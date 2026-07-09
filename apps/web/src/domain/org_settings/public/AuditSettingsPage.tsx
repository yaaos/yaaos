import { OrgSettingsLayout } from "@shared/components/public/layout/org-settings-layout";
import { AuditPage } from "../AuditPage";

export function AuditSettingsPage() {
  return (
    <OrgSettingsLayout active="audit">
      <AuditPage />
    </OrgSettingsLayout>
  );
}
