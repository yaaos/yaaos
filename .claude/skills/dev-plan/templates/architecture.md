# <one-line architecture summary>

## Approach

<short narrative of technical direction>

## Boundaries touched

- **Service boundaries:** <backend↔web, backend↔agent, etc.>
- **Module-within-service boundaries:** <module ↔ module>

## Entities & value objects

| Name | Kind | New/Changed | Lives in | Notes |
|---|---|---|---|---|
| <Entity> | entity / value object | new / changed | <service.module> | <one line> |

## Interface changes

### <Boundary A>

| Change | Signature / endpoint / payload / event | Notes |
|---|---|---|
| added | <sig> | <one line> |
| changed | <sig> | <was → is> |
| deleted | <sig> | <one line> |

<repeat per boundary>

## Sequence diagrams

<ASCII, one per affected boundary, only when call sequence changes. Mark entities. Embed inline AND save to diagrams/<name>.txt.>

<If no sequence changes: write "No sequence changes." and omit the diagrams/ directory entirely.>

## Data model changes

- **Tables:** <added / changed / dropped>
- **Columns:** <added / changed / dropped>
- **Migrations:** <forward + rollback notes>

## Open questions

- <architectural-level unknowns — distinct from intent.md and plan.md lists>
