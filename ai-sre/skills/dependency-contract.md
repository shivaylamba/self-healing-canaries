---
name: dependency-contract
summary: A call to another service fails because the response or request shape changed (API version migration, new or missing fields).
error_types: [inventory_lookup_failed, dependency_failed, decode_failed]
slice_keys: [checkout_sku]
template_regex: (missing|unexpected|decode|unmarshal|field|status \<\*\>|v\<\*\>)
path_globs: [apps/*/inventory*.go, apps/*/client*.go, apps/*/*api*.go]
path_kinds: [code]
revert_preference: fix_first
---
# Skill: dependency contract

Typical shape: a PR migrates to a new version of a dependency's API. Most requests work;
the ones whose data takes a shape the old code never saw fail (multi-warehouse SKUs,
optional fields, pagination, new enum values).

1. Group the canary's errors by `checkout_sku` (or the closest entity attribute). If they
   concentrate in a few entities, find what is different about those entities' responses.
2. Read the dependency's handler or schema in the repo (`apps/<dependency>/`) for the new
   version: which fields are optional, which shapes exist.
3. Read the caller in the offending change. Find the assumption the new shape breaks.
4. Fix the caller to accept every shape the new version returns. Keep the migration.
5. Guard: add a test with the shape that broke (for example a multi-warehouse response).
