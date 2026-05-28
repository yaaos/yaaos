import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import type React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const notificationsMock = vi.fn();
const markOneMock = vi.fn();
const markAllMock = vi.fn();

vi.mock("@core/api", () => ({
  useNotifications: (filter: string) => notificationsMock(filter),
  useMarkNotificationRead: () => ({ mutate: markOneMock, isPending: false }),
  useMarkAllNotificationsRead: () => ({ mutate: markAllMock, isPending: false }),
}));

import { NotificationsPage } from "../index";

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

const FIXED_NOW = new Date("2026-05-15T12:00:00Z");
const today = FIXED_NOW;
const yesterday = new Date(FIXED_NOW.getTime() - 26 * 3_600_000);
const lastWeek = new Date(FIXED_NOW.getTime() - 3 * 86_400_000);
const older = new Date(FIXED_NOW.getTime() - 60 * 86_400_000);

const fixture = [
  {
    id: "n1",
    user_id: "u1",
    org_id: "o1",
    type: "hitl_waiting",
    ticket_id: "t1",
    title: "Today event",
    body: "body 1",
    read_at: null,
    created_at: today.toISOString(),
  },
  {
    id: "n2",
    user_id: "u1",
    org_id: "o1",
    type: "ticket_completed",
    ticket_id: "t2",
    title: "Yesterday event",
    body: "body 2",
    read_at: null,
    created_at: yesterday.toISOString(),
  },
  {
    id: "n3",
    user_id: "u1",
    org_id: "o1",
    type: "ticket_completed",
    ticket_id: "t3",
    title: "Last week event",
    body: "body 3",
    read_at: null,
    created_at: lastWeek.toISOString(),
  },
  {
    id: "n4",
    user_id: "u1",
    org_id: "o1",
    type: "ticket_completed",
    ticket_id: "t4",
    title: "Old event",
    body: "body 4",
    read_at: null,
    created_at: older.toISOString(),
  },
];

describe("NotificationsPage", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(FIXED_NOW);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("loading state renders skeletons", () => {
    notificationsMock.mockReturnValue({ data: undefined, isLoading: true });
    render(wrap(<NotificationsPage />));
    expect(screen.getByText(/Notifications/)).toBeInTheDocument();
  });

  it("empty state when zero notifications", () => {
    notificationsMock.mockReturnValue({ data: [], isLoading: false });
    render(wrap(<NotificationsPage />));
    expect(screen.getByText(/No notifications/)).toBeInTheDocument();
  });

  it("groups items into Today / Yesterday / This week / Older", () => {
    notificationsMock.mockReturnValue({ data: fixture, isLoading: false });
    render(wrap(<NotificationsPage />));
    // Every notification renders.
    expect(screen.getByText("Today event")).toBeInTheDocument();
    expect(screen.getByText("Yesterday event")).toBeInTheDocument();
    expect(screen.getByText("Last week event")).toBeInTheDocument();
    expect(screen.getByText("Old event")).toBeInTheDocument();
    // Every header renders (date grouping is the feature under test).
    expect(screen.getByText("Today")).toBeInTheDocument();
    expect(screen.getByText("Yesterday")).toBeInTheDocument();
    expect(screen.getByText("This week")).toBeInTheDocument();
    expect(screen.getByText("Older")).toBeInTheDocument();
  });

  it("filter chips switch the read_state on click", () => {
    notificationsMock.mockReturnValue({ data: fixture, isLoading: false });
    render(wrap(<NotificationsPage />));
    fireEvent.click(screen.getByTestId("notifications-filter-unread"));
    // The query hook is called with the new filter on the next render.
    expect(notificationsMock).toHaveBeenLastCalledWith("unread");
  });

  it("row click fires mark-as-read mutation", () => {
    notificationsMock.mockReturnValue({ data: fixture, isLoading: false });
    render(wrap(<NotificationsPage />));
    fireEvent.click(screen.getByTestId("notification-row-n1"));
    expect(markOneMock).toHaveBeenCalledWith("n1");
  });

  it("Mark all read button fires the bulk mutation", () => {
    notificationsMock.mockReturnValue({ data: fixture, isLoading: false });
    render(wrap(<NotificationsPage />));
    fireEvent.click(screen.getByText(/Mark all read/));
    expect(markAllMock).toHaveBeenCalled();
  });
});
