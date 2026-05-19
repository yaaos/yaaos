/**
 * Current org context. Routes update this in their `beforeLoad`; the
 * `apiFetch` wrapper reads it and injects `X-Org-Slug` so individual
 * domain hooks don't need to thread the slug through every call site.
 *
 * Stored as a module-global because:
 *  - React context can't reach the `apiFetch` plain function.
 *  - The slug is part of the URL, so it's already global state.
 *  - Tests can `setCurrentOrgSlug(...)` directly.
 */

let _slug: string | null = null;

export function setCurrentOrgSlug(slug: string | null): void {
  _slug = slug;
}

export function getCurrentOrgSlug(): string | null {
  return _slug;
}
