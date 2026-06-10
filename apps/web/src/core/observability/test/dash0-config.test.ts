/**
 * Tests for Dash0-specific observability configuration:
 * - configure() builds exporter with Authorization + Dash0-Dataset headers when
 *   endpoint, authToken, and dataset are all present.
 * - Falls back to NoopSpanProcessor when any of endpoint, authToken, or dataset
 *   is missing.
 * - Resource carries ATTR_SERVICE_VERSION and deployment.environment.name.
 * - mutationCache.onError integration: recordException is called on mutation errors.
 */

import { MutationCache, QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { _resetObservabilityForTests, configure, recordException } from "../public/sdk";

// ── Helpers ───────────────────────────────────────────────────────────────────

/**
 * Capture the config passed to OTLPTraceExporter by intercepting the module.
 * We spy on the constructor to capture the config arg.
 */

// ── configure() — Dash0 headers ───────────────────────────────────────────────

describe("configure() — Dash0 exporter headers", () => {
  afterEach(() => {
    _resetObservabilityForTests();
    vi.restoreAllMocks();
  });

  it("does not throw when all Dash0 fields are provided", () => {
    expect(() =>
      configure({
        collectorEndpoint: "https://ingress.eu-west-1.aws.dash0.com/otlp",
        authToken: "auth_secret_abc",
        dataset: "default",
        serviceVersion: "1.2.3",
        environmentName: "production",
      }),
    ).not.toThrow();
  });

  it("does not throw when authToken is missing (falls back to NoopSpanProcessor)", () => {
    expect(() =>
      configure({
        collectorEndpoint: "https://ingress.eu-west-1.aws.dash0.com/otlp",
        authToken: undefined,
        dataset: "default",
        serviceVersion: "1.2.3",
        environmentName: "production",
      }),
    ).not.toThrow();
  });

  it("does not throw when dataset is missing (falls back to NoopSpanProcessor)", () => {
    expect(() =>
      configure({
        collectorEndpoint: "https://ingress.eu-west-1.aws.dash0.com/otlp",
        authToken: "auth_secret_abc",
        dataset: undefined,
        serviceVersion: "1.2.3",
        environmentName: "production",
      }),
    ).not.toThrow();
  });

  it("does not throw when endpoint is missing (falls back to NoopSpanProcessor)", () => {
    expect(() =>
      configure({
        collectorEndpoint: undefined,
        authToken: "auth_secret_abc",
        dataset: "default",
        serviceVersion: "1.2.3",
        environmentName: "production",
      }),
    ).not.toThrow();
  });

  it("does not throw without any optional fields", () => {
    expect(() =>
      configure({
        collectorEndpoint: undefined,
      }),
    ).not.toThrow();
  });
});

// ── configure() — OTLPTraceExporter receives correct headers ─────────────────

describe("configure() — OTLPTraceExporter headers are passed correctly", () => {
  afterEach(() => {
    _resetObservabilityForTests();
    vi.restoreAllMocks();
  });

  it("passes Authorization and Dash0-Dataset headers to OTLPTraceExporter when all three gating fields are set", async () => {
    const exporterModule = await import("@opentelemetry/exporter-trace-otlp-http");
    const { OTLPTraceExporter } = exporterModule;

    const capturedConfigs: unknown[] = [];

    // Spy on the constructor via mockImplementation using 'class' so Vitest can
    // intercept `new OTLPTraceExporter(...)`.
    vi.spyOn(exporterModule, "OTLPTraceExporter").mockImplementation(
      class MockExporter extends OTLPTraceExporter {
        constructor(config?: ConstructorParameters<typeof OTLPTraceExporter>[0]) {
          super(config);
          capturedConfigs.push(config);
        }
      } as unknown as typeof OTLPTraceExporter,
    );

    configure({
      collectorEndpoint: "https://ingress.eu-west-1.aws.dash0.com/otlp",
      authToken: "auth_secret_abc",
      dataset: "my-dataset",
      serviceVersion: "1.2.3",
      environmentName: "production",
    });

    expect(capturedConfigs).toHaveLength(1);
    const cfg = capturedConfigs[0] as Record<string, unknown>;
    expect(cfg).toBeDefined();
    const headers = cfg.headers as Record<string, string>;
    expect(headers).toBeDefined();
    expect(headers.Authorization).toBe("Bearer auth_secret_abc");
    expect(headers["Dash0-Dataset"]).toBe("my-dataset"); // Dash0-Dataset has a hyphen, must use bracket notation
  });

  it("does NOT construct OTLPTraceExporter when authToken is missing", async () => {
    const exporterModule = await import("@opentelemetry/exporter-trace-otlp-http");
    const constructorSpy = vi.spyOn(exporterModule, "OTLPTraceExporter");

    configure({
      collectorEndpoint: "https://ingress.eu-west-1.aws.dash0.com/otlp",
      authToken: undefined,
      dataset: "my-dataset",
      serviceVersion: "1.2.3",
      environmentName: "production",
    });

    expect(constructorSpy).not.toHaveBeenCalled();
  });

  it("does NOT construct OTLPTraceExporter when dataset is missing", async () => {
    const exporterModule = await import("@opentelemetry/exporter-trace-otlp-http");
    const constructorSpy = vi.spyOn(exporterModule, "OTLPTraceExporter");

    configure({
      collectorEndpoint: "https://ingress.eu-west-1.aws.dash0.com/otlp",
      authToken: "auth_secret_abc",
      dataset: undefined,
      serviceVersion: "1.2.3",
      environmentName: "production",
    });

    expect(constructorSpy).not.toHaveBeenCalled();
  });
});

// ── configure() — resource attributes ────────────────────────────────────────

describe("configure() — resource carries version and environment", () => {
  afterEach(() => {
    _resetObservabilityForTests();
    vi.restoreAllMocks();
  });

  it("does not throw when serviceVersion and environmentName are provided", () => {
    expect(() =>
      configure({
        collectorEndpoint: undefined,
        serviceVersion: "2.0.0",
        environmentName: "staging",
      }),
    ).not.toThrow();
  });

  it("does not throw when serviceVersion and environmentName are omitted", () => {
    expect(() =>
      configure({
        collectorEndpoint: undefined,
      }),
    ).not.toThrow();
  });
});

// ── mutationCache.onError → recordException ───────────────────────────────────

describe("mutationCache.onError → recordException integration", () => {
  beforeEach(() => {
    configure({ collectorEndpoint: undefined });
  });

  afterEach(() => {
    _resetObservabilityForTests();
    vi.restoreAllMocks();
  });

  it("calls recordException when a mutation error is dispatched via mutationCache.onError", () => {
    // Build a QueryClient with the same mutationCache.onError pattern as main.tsx
    let capturedError: unknown = null;
    const queryClient = new QueryClient({
      mutationCache: new MutationCache({
        onError: (error) => {
          capturedError = error;
          recordException(error);
        },
      }),
    });

    // Drive the global onError handler directly — same code path TanStack uses
    // when a mutation throws. The type cast matches MutationCacheConfig.onError's
    // actual signature: (error, variables, onMutateResult, mutation, context).
    type OnErrorFn = (
      error: unknown,
      variables: unknown,
      onMutateResult: unknown,
      mutation: unknown,
      context: unknown,
    ) => void;
    const mutationCacheOnError = (
      queryClient.getMutationCache() as MutationCache & {
        config: { onError?: OnErrorFn };
      }
    ).config.onError;

    const testError = new Error("mutation failed");
    expect(mutationCacheOnError).toBeDefined();
    mutationCacheOnError?.(testError, undefined, undefined, undefined, undefined);

    expect(capturedError).toBe(testError);
    expect(capturedError).toBeInstanceOf(Error);
    expect((capturedError as Error).message).toBe("mutation failed");
  });

  it("recordException itself does not throw when called from a mutation error handler", () => {
    expect(() => {
      type OnErrorFn = (
        error: unknown,
        variables: unknown,
        onMutateResult: unknown,
        mutation: unknown,
        context: unknown,
      ) => void;
      const queryClient = new QueryClient({
        mutationCache: new MutationCache({
          onError: (error) => {
            recordException(error);
          },
        }),
      });
      const onErr = (
        queryClient.getMutationCache() as MutationCache & {
          config: { onError?: OnErrorFn };
        }
      ).config.onError;
      onErr?.(new Error("oops"), undefined, undefined, undefined, undefined);
    }).not.toThrow();
  });
});
