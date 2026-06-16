# Dead test — `resolve_skill`, `set_repo_skill`, `list_repos_with_skill`, and
# `build_review_invocation` were removed from the `ClaudeCodePlugin` surface
# when the legacy per-repo skill resolution was retired. The plugin now exposes
# only `build_invocation` (which inlines prompt building) and `parse_result`.
