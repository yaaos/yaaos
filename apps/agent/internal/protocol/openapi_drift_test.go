package protocol

// Drift-detection: openapi/agent-api.yaml ↔ hand-written Go mirror.
//
// Mirror of `app/core/agent_gateway/test/test_openapi_mirror_drift.py`
// on the Go side. The OpenAPI spec is the contract; this
// file's structs are the hand-written Go mirror. The test walks every
// schema in the spec that has a Go mirror, resolves allOf + $ref
// composition, and asserts every YAML property name appears as a
// `json:` tag on the matching Go struct.
//
// Type checking is intentionally light — name presence catches 90% of
// drift (rename, add, remove) with minimal maintenance cost. The matching
// Python drift test catches the type-level parts (Pydantic field types,
// Literal[…] discriminator values) on the backend half.

import (
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"sort"
	"testing"

	"gopkg.in/yaml.v3"
)

// schemaSpec is a minimal subset of the OpenAPI document we need.
type openapiSpec struct {
	Components struct {
		Schemas map[string]schemaNode `yaml:"schemas"`
	} `yaml:"components"`
}

// schemaNode covers the fields we read off each schema. The full OpenAPI
// shape is broader; we only need allOf + $ref + properties + enum.
type schemaNode struct {
	Type          string                `yaml:"type"`
	Properties    map[string]schemaNode `yaml:"properties"`
	Required      []string              `yaml:"required"`
	AllOf         []schemaNode          `yaml:"allOf"`
	Ref           string                `yaml:"$ref"`
	Enum          []string              `yaml:"enum"`
	Discriminator struct {
		PropertyName string            `yaml:"propertyName"`
		Mapping      map[string]string `yaml:"mapping"`
	} `yaml:"discriminator"`
}

// schemaToStruct maps each spec schema name to (Go reflect type, fields
// to skip). Skipping is for fields that are spec-only — none yet on the
// Go side, kept for symmetry with the Python test.
var schemaToStruct = map[string]struct {
	t          reflect.Type
	skipFields map[string]struct{}
}{
	"CommandEventAck":             {reflect.TypeOf(CommandEventAck{}), nil},
	"AgentMetadata":               {reflect.TypeOf(AgentMetadata{}), nil},
	"IdentityExchangeRequest":     {reflect.TypeOf(IdentityExchangeRequest{}), nil},
	"IdentityExchangeResponse":    {reflect.TypeOf(IdentityExchangeResponse{}), nil},
	"HeartbeatRequest":            {reflect.TypeOf(HeartbeatRequest{}), nil},
	"HeartbeatWorkspaceEntry":     {reflect.TypeOf(HeartbeatWorkspaceEntry{}), nil},
	"HeartbeatResponse":           {reflect.TypeOf(HeartbeatResponse{}), nil},
	"ClaimRequest":                {reflect.TypeOf(ClaimRequest{}), nil},
	"CommandBase":                 {reflect.TypeOf(CommandHeader{}), nil},
	"ProvisionWorkspaceCommand":   {reflect.TypeOf(ProvisionWorkspaceCommand{}), nil},
	"WriteFilesCommand":           {reflect.TypeOf(WriteFilesCommand{}), nil},
	"RefreshWorkspaceAuthCommand": {reflect.TypeOf(RefreshWorkspaceAuthCommand{}), nil},
	"InvokeClaudeCodeCommand":     {reflect.TypeOf(InvokeClaudeCodeCommand{}), nil},
	"CleanupWorkspaceCommand":     {reflect.TypeOf(CleanupWorkspaceCommand{}), nil},
	"AgentConfig":                 {reflect.TypeOf(AgentConfigWire{}), nil},
	"ConfigUpdateCommand":         {reflect.TypeOf(ConfigUpdateCommand{}), nil},
	"AgentEvent":                  {reflect.TypeOf(AgentEvent{}), nil},
}

// skipped — schemas the test deliberately doesn't mirror to a Go struct.
// ErrorEnvelope is HTTP error shape (handled inline in the client);
// AgentCommand union dispatch lives in AgentCommand.UnmarshalJSON which
// reads `kind` and switches over the five concrete kinds.
// WorkspaceEvent is a backend-side type; the agent never emits these events
// so the Go mirror was removed while the spec entry remains for the backend.
var skippedSchemas = map[string]struct{}{
	"ErrorEnvelope":  {},
	"AgentCommand":   {},
	"WorkspaceEvent": {},
}

// specPath resolves to apps/backend/openapi/agent-api.yaml relative to
// this test file. Computed once at package load.
func specPath(t *testing.T) string {
	t.Helper()
	_, thisFile, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("runtime.Caller failed")
	}
	// thisFile: apps/agent/internal/protocol/openapi_drift_test.go
	// spec:     apps/backend/openapi/agent-api.yaml
	return filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "backend", "openapi", "agent-api.yaml")
}

func loadSpec(t *testing.T) openapiSpec {
	t.Helper()
	data, err := os.ReadFile(specPath(t))
	if err != nil {
		t.Fatalf("read spec: %v", err)
	}
	var spec openapiSpec
	if err := yaml.Unmarshal(data, &spec); err != nil {
		t.Fatalf("unmarshal spec: %v", err)
	}
	return spec
}

// resolveProperties flattens allOf composition + $ref into a single
// property map. Mirrors the Python helper.
func resolveProperties(spec openapiSpec, node schemaNode) map[string]schemaNode {
	out := map[string]schemaNode{}
	for _, part := range node.AllOf {
		if part.Ref != "" {
			refName := refTail(part.Ref)
			if resolved, ok := spec.Components.Schemas[refName]; ok {
				for k, v := range resolveProperties(spec, resolved) {
					out[k] = v
				}
			}
			continue
		}
		for k, v := range resolveProperties(spec, part) {
			out[k] = v
		}
	}
	for k, v := range node.Properties {
		out[k] = v
	}
	return out
}

func refTail(ref string) string {
	// `#/components/schemas/Foo` → `Foo`
	for i := len(ref) - 1; i >= 0; i-- {
		if ref[i] == '/' {
			return ref[i+1:]
		}
	}
	return ref
}

// jsonTagNames returns the JSON-tag names declared on a struct.
// `json:"foo,omitempty"` → "foo". Embedded structs are flattened so a
// type embedding CommandHeader picks up its command_id/workspace_id/
// traceparent/kind tags.
func jsonTagNames(t reflect.Type) map[string]struct{} {
	out := map[string]struct{}{}
	if t.Kind() != reflect.Struct {
		return out
	}
	for i := 0; i < t.NumField(); i++ {
		f := t.Field(i)
		if f.Anonymous {
			// Embedded struct — flatten its tags into the outer set.
			for k := range jsonTagNames(f.Type) {
				out[k] = struct{}{}
			}
			continue
		}
		tag := f.Tag.Get("json")
		if tag == "" || tag == "-" {
			continue
		}
		// strip ",omitempty" and friends
		for i := 0; i < len(tag); i++ {
			if tag[i] == ',' {
				tag = tag[:i]
				break
			}
		}
		if tag != "" {
			out[tag] = struct{}{}
		}
	}
	return out
}

// ── Tests ───────────────────────────────────────────────────────────────

func TestOpenAPIDrift_EverySchemaHasAKnownHandler(t *testing.T) {
	// Silent-addition guard: when someone adds a new schema to the
	// YAML, this test fails until the mapping (or the explicit skip
	// set) is updated.
	spec := loadSpec(t)
	for name := range spec.Components.Schemas {
		_, mapped := schemaToStruct[name]
		_, skipped := skippedSchemas[name]
		if !mapped && !skipped {
			t.Errorf("OpenAPI schema %q has no Go mirror declared in "+
				"`schemaToStruct` or `skippedSchemas`", name)
		}
	}
}

func TestOpenAPIDrift_GoMirrorHasEveryYAMLProperty(t *testing.T) {
	spec := loadSpec(t)

	// Iterate in a stable order so failures are reproducible.
	names := make([]string, 0, len(schemaToStruct))
	for k := range schemaToStruct {
		names = append(names, k)
	}
	sort.Strings(names)

	for _, schemaName := range names {
		mapping := schemaToStruct[schemaName]
		node, ok := spec.Components.Schemas[schemaName]
		if !ok {
			t.Errorf("schema %q listed in schemaToStruct but missing from spec", schemaName)
			continue
		}
		yamlProps := resolveProperties(spec, node)
		goFields := jsonTagNames(mapping.t)

		var missing []string
		for prop := range yamlProps {
			if _, skip := mapping.skipFields[prop]; skip {
				continue
			}
			if _, ok := goFields[prop]; !ok {
				missing = append(missing, prop)
			}
		}
		if len(missing) > 0 {
			sort.Strings(missing)
			t.Errorf("schema %q: YAML properties %v are NOT mirrored on Go type %s (have json tags %v). "+
				"Add struct fields with matching `json:` tags or update `schemaToStruct[%q].skipFields`.",
				schemaName, missing, mapping.t.Name(), sortedKeys(goFields), schemaName)
		}
	}
}

func TestOpenAPIDrift_AgentCommandKindsMatchSpecMapping(t *testing.T) {
	spec := loadSpec(t)
	yamlKinds := spec.Components.Schemas["AgentCommand"].Discriminator.Mapping
	goKinds := map[string]struct{}{
		string(KindProvisionWorkspace):   {},
		string(KindWriteFiles):           {},
		string(KindRefreshWorkspaceAuth): {},
		string(KindInvokeClaudeCode):     {},
		string(KindCleanupWorkspace):     {},
		string(KindConfigUpdate):         {},
	}
	for k := range yamlKinds {
		if _, ok := goKinds[k]; !ok {
			t.Errorf("AgentCommand discriminator %q in spec is not in CommandKind consts", k)
		}
	}
	for k := range goKinds {
		if _, ok := yamlKinds[k]; !ok {
			t.Errorf("CommandKind const %q has no matching spec discriminator mapping", k)
		}
	}
}

func TestOpenAPIDrift_AgentEventKindsMatchSpecEnum(t *testing.T) {
	spec := loadSpec(t)
	var yamlKinds []string
	if kind, ok := spec.Components.Schemas["AgentEvent"].Properties["kind"]; ok {
		yamlKinds = kind.Enum
	}
	goKinds := map[string]struct{}{
		string(EventProgress):         {},
		string(EventReceived):         {},
		string(EventCompletedSuccess): {},
		string(EventCompletedFailure): {},
		string(EventCompletedSkipped): {},
	}
	for _, k := range yamlKinds {
		if _, ok := goKinds[k]; !ok {
			t.Errorf("AgentEvent.kind enum value %q in spec is not in EventKind consts", k)
		}
	}
	if len(yamlKinds) != len(goKinds) {
		t.Errorf("AgentEvent.kind drift: spec has %d values, Go has %d", len(yamlKinds), len(goKinds))
	}
}

// sortedKeys returns map keys in sorted order — used to make the missing-
// field failure message reproducible across runs.
func sortedKeys(m map[string]struct{}) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
