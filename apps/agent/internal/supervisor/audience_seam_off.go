//go:build !agent_dev

package supervisor

// audienceOverride returns the audience to use for the signed STS claim,
// overriding the value derived from BaseURL.
//
// In the production binary this always returns "": the derived audience
// (hostFromURL(BaseURL)) is used unconditionally. The env-var-honoring variant
// lives in audience_seam_on.go and is compiled only with `-tags agent_dev`, so
// the production binary has no code path that reads YAAOS_AGENT_AUDIENCE_OVERRIDE
// — the override cannot be enabled regardless of how the environment is configured.
func audienceOverride() string { return "" }
