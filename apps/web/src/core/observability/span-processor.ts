/**
 * Stamps yaaos.org_id and yaaos.user_id on every web-originating span.
 *
 * Reads from the module-scope identity holder (set via setIdentity after auth
 * resolves) at onStart time — no baggage, no cross-wire identity claims.
 * Backend stamps its own spans authoritatively from session context.
 */

import type { Context } from "@opentelemetry/api";
import type { ReadableSpan, Span, SpanProcessor } from "@opentelemetry/sdk-trace-web";
import { getIdentity } from "./identity";

export class YaaosSpanProcessor implements SpanProcessor {
  onStart(span: Span, _parentContext: Context): void {
    const id = getIdentity();
    if (id) {
      span.setAttribute("yaaos.org_id", id.orgId);
      span.setAttribute("yaaos.user_id", id.userId);
    }
  }

  onEnd(_span: ReadableSpan): void {
    // Delegation to the downstream processor (exporter) is handled by
    // WebTracerProvider's pipeline — this processor only stamps; it does not
    // export.
  }

  forceFlush(): Promise<void> {
    return Promise.resolve();
  }

  shutdown(): Promise<void> {
    return Promise.resolve();
  }
}
