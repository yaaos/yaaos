import "@testing-library/jest-dom/vitest";
import { afterAll, afterEach, beforeAll } from "vitest";
import { server } from "./test/msw/server";

beforeAll(() => server.listen({ onUnhandledRequest: "warn" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

// jsdom doesn't implement the Pointer Events capture API or `scrollIntoView` —
// Radix primitives that use a Portal + pointer-driven open/close (Select,
// DropdownMenu) call these unconditionally and throw without a stub. No-op
// polyfills are enough; tests never assert on capture/scroll behavior itself.
for (const method of ["hasPointerCapture", "setPointerCapture", "releasePointerCapture"] as const) {
  if (!Element.prototype[method]) {
    Element.prototype[method] = () => false;
  }
}
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

// Root `package.json`'s `pnpm.overrides` pins every shared Radix internal
// package (`react-focus-scope`, `react-dismissable-layer`, `react-dialog`,
// `react-presence`, `react-primitive`, …) to one resolved version
// workspace-wide. Each of these coordinates sibling overlay instances via
// module-scoped state (a focus-trap stack, a `Set` of layers holding the
// body pointer-events lock, …); two overlays backed by two DIFFERENT
// resolved copies of the same package can't see each other's state:
//   - `react-focus-scope`: a DropdownMenu closing the same tick a Sheet
//     opens (as `PipelineEditor`'s "Add stage" picker does) makes two
//     focus-scopes fight over `document.activeElement`, which recurses
//     infinitely in jsdom's focus dispatch (real browsers don't hit this).
//   - `react-dismissable-layer`: the same interaction can strand
//     `document.body.style.pointerEvents = "none"` forever in a *real*
//     browser too — one module instance's lock captures the other's
//     already-locked value as "the original" to restore, so the restore
//     never actually clears it. This one is real-browser-visible and is
//     what `pipeline-settings-crud.spec.ts` (e2e) caught.
// Without the override, which duplicate version each primitive resolves to
// is an accident of when it was `pnpm add`-ed — pin them all rather than
// whack-a-mole discovering each interaction that trips over a mismatch.
