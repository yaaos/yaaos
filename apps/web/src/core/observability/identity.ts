/**
 * Module-scope identity holder for OTel span stamping.
 *
 * Set after auth resolves via setIdentity(). Read by YaaosSpanProcessor.onStart
 * to stamp yaaos.org_id / yaaos.user_id on web-originating spans only.
 * Identity never leaves the browser — no baggage header is set.
 */

export interface YaaosIdentity {
  orgId: string;
  userId: string;
}

let _identity: YaaosIdentity | null = null;

/** Set or clear the current authenticated identity. Call after auth resolves. */
export function setIdentity(identity: YaaosIdentity | null): void {
  _identity = identity;
}

/** Read the current identity. Returns null if not yet authenticated. */
export function getIdentity(): YaaosIdentity | null {
  return _identity;
}

/** Reset for tests only. */
export function _resetIdentityForTests(): void {
  _identity = null;
}
