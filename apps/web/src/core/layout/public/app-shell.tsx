import { useOtelIdentitySync } from "@core/observability/public/use-otel-identity-sync";
import { Sidebar } from "@core/sidebar/public/sidebar";
import { useServerEvents } from "@core/sse/public/subscriber";
import { Skeleton } from "@shared/components/ui/skeleton";
import { Outlet, useRouterState } from "@tanstack/react-router";
import { Suspense, useEffect, useRef } from "react";
import { BrokenIntegrationsBanner } from "../broken-integrations-banner";

/** Sidebar-shaped skeleton shown while UserCard/RequireMembership suspense resolves. */
function SidebarSkeleton() {
  return (
    <aside
      className="flex flex-col bg-card border-r border-border shrink-0 w-[56px]"
      data-testid="sidebar-loading"
    >
      <div className="flex justify-center items-center h-[56px] border-b border-border">
        <Skeleton className="h-7 w-7 rounded" />
      </div>
      <div className="px-1.5 py-2 border-b border-border">
        <Skeleton className="h-7 w-full rounded" />
      </div>
      <div className="flex flex-col gap-1.5 px-1.5 py-2 flex-1">
        <Skeleton className="h-7 w-full rounded" />
        <Skeleton className="h-7 w-full rounded" />
        <Skeleton className="h-7 w-full rounded" />
      </div>
      <div className="px-1.5 py-2 border-t border-border">
        <Skeleton className="h-7 w-full rounded" />
      </div>
    </aside>
  );
}

// Two routes render outside the shell: `/login` (anonymous, no nav) and
// `/orgs` (the picker — explicit "no org selected" surface). Every other
// authenticated route lives under `/orgs/$slug/...` and gets the sidebar.
const STANDALONE_PATHS = new Set(["/login", "/orgs"]);

export function AppShell() {
  // Owns the browser-wide general-event SSE stream; (re)targets it at the
  // active org as the route changes. Called unconditionally (before the
  // standalone-path early return) to respect the rules-of-hooks.
  useServerEvents();
  // Syncs authenticated user identity into the OTel holder so spans carry
  // yaaos.org_id / yaaos.user_id. Called unconditionally per rules-of-hooks.
  useOtelIdentitySync();

  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const mainRef = useRef<HTMLElement | null>(null);

  // On every route change, move keyboard focus to the first heading in <main>
  // (if present) or to <main> itself, so screen-reader and keyboard users land
  // at the top of the new page. tabIndex={-1} on <main> makes it
  // programmatically focusable without adding it to the tab order.
  //
  // Three timing hazards make a single synchronous focus() insufficient:
  //   1. A link click commits the route, but the browser's own click default
  //      action focuses the clicked <a> *afterward*, overriding us.
  //   2. Data pages render under <Suspense>, so the <h1> — and on a cold boot
  //      even <main> itself — isn't in the DOM on the frame the route commits;
  //      it arrives once the auth/org/page queries resolve.
  //   3. The effect keys on pathname, which doesn't change again while the
  //      shell finishes mounting, so a one-shot attempt that runs before
  //      <main> exists would never retry.
  // So we poll across a short budget of frames until focus is inside <main>,
  // then stop. The `contains` check means we never steal focus a page
  // legitimately placed on an input inside <main> (e.g. an autofocused field).
  useEffect(() => {
    if (!pathname) return;
    let frame = 0;
    let raf = 0;
    const place = () => {
      const main = mainRef.current;
      if (main && !main.contains(document.activeElement)) {
        // Prefer the page's <h1> so a screen reader announces the heading, but
        // a bare <h1> isn't focusable — give it tabIndex={-1} first (idempotent
        // and out of the tab order). Fall back to <main> when there's no
        // heading yet (e.g. a Suspense fallback is still showing).
        const h1 = main.querySelector<HTMLElement>("h1");
        if (h1 && !h1.hasAttribute("tabindex")) h1.setAttribute("tabindex", "-1");
        (h1 ?? main).focus({ preventScroll: false });
      }
      frame += 1;
      // ~45 frames (~750ms) covers the click-focus, a Suspense resolve, and a
      // cold-boot shell mount where <main> appears a few hundred ms in.
      if (frame < 45 && !main?.contains(document.activeElement)) {
        raf = requestAnimationFrame(place);
      }
    };
    raf = requestAnimationFrame(place);
    return () => cancelAnimationFrame(raf);
  }, [pathname]);

  if (STANDALONE_PATHS.has(pathname)) {
    return (
      <div className="h-screen w-screen overflow-y-auto">
        <Outlet />
      </div>
    );
  }

  return (
    <div className="flex h-screen w-screen overflow-hidden">
      <Suspense fallback={<SidebarSkeleton />}>
        <Sidebar />
      </Suspense>
      <div className="flex flex-col flex-1 min-w-0">
        <BrokenIntegrationsBanner />
        {/* tabIndex={-1} makes <main> programmatically focusable for focus-reset
            without placing it in the natural tab order. */}
        <main ref={mainRef} tabIndex={-1} className="flex-1 overflow-y-auto p-6 outline-none">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
