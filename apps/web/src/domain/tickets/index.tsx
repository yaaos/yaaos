/**
 * Tickets domain — M06 anchor pages.
 *
 * The list page (E2a.1) lives at `./TicketsListPage.tsx`; the detail page
 * (E2a.4) lives at `./TicketDetailPage.tsx`. Both pages compose the
 * standalone composites in this folder (`StageIndicator`, `HitlPanel`,
 * `FindingRow`, `ActivityEventRow`).
 *
 * This barrel exists only to expose `TicketsPage` + `TicketDetailPage`
 * for the router under stable names; everything else is intra-folder.
 */

export { TicketsListPage as TicketsPage } from "./TicketsListPage";
export { TicketDetailPage } from "./TicketDetailPage";
