import { AuditPage } from "../orgs/AuditPage";
import { OrgSettingsLayout } from "./OrgSettingsLayout";

export function AuditSettingsPage() {
  return (
    <OrgSettingsLayout active="audit">
      <AuditPage />
    </OrgSettingsLayout>
  );
}
