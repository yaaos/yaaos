import { OrgSettingsLayout } from "./OrgSettingsLayout";

interface Props {
  active: string;
  title: string;
  phase: string;
}

/**
 * Placeholder for sections that have route + tab wiring in Phase 7 but whose
 * real content lands in Phase 8 (VCS), Phase 9 (Coding Agents), or Phase 11
 * (BYOK). Renders a one-line "coming soon" so links don't 404 mid-milestone.
 */
export function PlaceholderSettingsPage({ active, title, phase }: Props) {
  return (
    <OrgSettingsLayout active={active}>
      <div className="p-6">
        <h2 className="text-[16px] font-semibold mb-2">{title}</h2>
        <p className="text-text-3 text-sm" data-testid={`placeholder-${active}`}>
          Lands in {phase}.
        </p>
      </div>
    </OrgSettingsLayout>
  );
}
