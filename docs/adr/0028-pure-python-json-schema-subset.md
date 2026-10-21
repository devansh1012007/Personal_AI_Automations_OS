# ADR-0028: A pure-Python JSON Schema subset, with boolean schemas honoured

## Status

Accepted.

> Written on 2026-10-21 to close a citation that pointed at the wrong ADR.
> The decision itself was taken with the first implementation; the *record* of
> it was missing, and `src/paa/validation/` cited `docs/adr/0010` — which is
> [ADR-0010](0010-head-projection-not-partial-index.md), a decision about the
> ledger's head projection and about `EXECUTION_COMPLETED` not being
> terminal. Nothing in ADR-0010 has ever described a schema. The number
> belonged to ADR-0010 already, so this record takes the next free one rather
> than renumbering a decision that other documents cite.

## Context

RFC §13 requires every host-side check to be **deterministic**: a candidate
mutation is committed or it is not, and no model gets a vote. Two boundaries
need a structural validator, and both are security controls rather than
conveniences:

* the **input gate** — the Unified Skill Adapter validates a skill's
  arguments against the `input_schema` its contract declares (RFC §8.2
  step 2), before anything runs;
* the **output gate** — the same adapter validates what the skill returned
  against the declared `output_schema` (RFC §8.2 step 6), which is the
  runtime's single output gate (RFC §13).

A payload the validator lets through is a payload the runtime acts on.

The obvious way to get a JSON Schema validator is to depend on `jsonschema`.
The problem is what happens when it is *absent*.

`jsonschema` is not a declared dependency of this project: the runtime
targets a memory-constrained machine and every heavyweight dependency is
opt-in (see the extras in `pyproject.toml`). A conditional import would
therefore make the strictness of a security gate depend on whether an
*optional, unrelated* extra happened to be installed — the same artifact
passing on one machine and failing on another, with no configuration
difference to point at. The failure mode is asymmetric, and that is what
makes it unacceptable: the lenient path is the one that ships, because
"the library is missing" reads as "there is nothing to check", and "it is
stricter when the library is present" is the worst of both worlds.

So the subset is implemented here, in pure Python, in two places, because the
two boundaries answer different questions:

* `validation/schema_validator.py` — `SchemaValidator`, the runtime's output
  gate. The wider subset: it judges a payload against a contract's declared
  schema, so it implements the keywords that appear in practice.
* `skills/contracts.py` — `schema_errors`, the contract-side validator the
  Unified Skill Adapter runs against a skill's declared `input_schema`. The
  narrower subset: it constrains a skill's arguments and nothing else.

## Decision

**Implement the JSON Schema subset in pure Python, and support the boolean
schemas in full.**

A schema node is either a mapping of keywords or one of the two boolean
schemas. Both forms are legal JSON Schema, and the boolean form is the half
that carries the security-relevant meaning:

* `true` accepts every instance;
* `false` **refuses every instance** — as the root schema, as a property
  schema, as an item schema, or inside `anyOf` / `oneOf` / `allOf` / `not`.

`false` is not an exotic edge case. It is the schema a contract author writes
when a value must not appear, and a validator that cannot represent it will
accept the very payload it was written to reject. The alias
`JsonSchema = dict[str, Any] | bool` states this in the signature, so a
caller cannot hand the validator a boolean without the type checker seeing
it.

Keywords outside the supported set are **ignored, not guessed** — an
unimplemented keyword that reads as "valid" is a validator that lies. Five
keywords are never merely ignored, because ignoring them would leave a
constraint the author believes is in force unenforced: `$ref`, `$dynamicRef`,
`if`, `then` and `else` are rejected outright by `SchemaValidator.validate`.
`SchemaValidator(strict_keywords=True)` rejects *every* unsupported keyword,
for callers that want the schema to be fully understood before it is trusted.

### The subset, exactly

`SchemaValidator` understands `type` (a name or a list of names),
`properties`, `required`, `additionalProperties` (boolean or schema),
`items`, `prefixItems`, `enum`, `const`, `minimum`, `maximum`,
`exclusiveMinimum`, `exclusiveMaximum`, `multipleOf`, `minLength`,
`maxLength`, `pattern`, `minItems`, `maxItems`, `uniqueItems`,
`minProperties`, `maxProperties`, `anyOf`, `oneOf`, `allOf`, `not`, and the
two boolean schemas. `nullable` is a repository extension: it lets a `null`
value satisfy a `type` that would otherwise reject it. The annotation-only
keywords `default`, `title`, `description`, `examples`, `$schema`, `$id` and
`$comment` are accepted and carry no constraint. `definitions` and `$defs`
are accepted too, but they are containers for reusable subschemas and
`$ref` is not supported, so nothing in them is ever applied.

`schema_errors` understands a smaller set: `type`, `properties`, `required`,
`additionalProperties` (the boolean form only — the schema form is ignored),
`enum`, `items`, `pattern`, `minimum`, `maximum`, `minLength`, `maxLength`,
`minItems`, `maxItems`, and the two boolean schemas.

## Consequences

- **The gate is present in every install.** There is no configuration under
  which a skill's output is judged by a different rule than the one this
  repository documents, and no extra to install before the control exists.
- **The subset is not JSON Schema.** A schema that uses an unsupported
  keyword is accepted with that keyword *ignored*, so the gate is only as
  strict as the keywords it implements. `strict_keywords=True` turns the
  silence into a failure; nothing in `src/` enables it today, so the screen
  that rejects `$ref` and friends is reachable only through
  `SchemaValidator.validate`, not through the `check` that the skill output
  gate uses.
- **`false` is honoured at every depth.** Before 2026-10-21 the `false`
  branch sat *behind* a shape guard — `isinstance(False, dict)` is `False`,
  so a "is this a mapping?" check ahead of it swallowed the forbidding schema
  and returned "no errors". A `false` schema therefore accepted every
  payload, and a `false` item schema was skipped entirely, so the array
  validated as though it had no item constraints at all. The behaviour
  described above is the intended one; the guard's position was the defect,
  and it is fixed.
- **`not: false` accepts everything.** `not` requires the instance to *fail*
  the subschema, and `false` fails every instance, so every instance
  satisfies `not: false`. This is JSON Schema's meaning, not a special case
  — and it is the one place where honouring `false` made the validator
  *more* permissive than before, because `false` used to read as "matches
  everything", which made `not: false` reject everything.
- **Two implementations, one policy.** The narrow `schema_errors` subset and
  the wide `SchemaValidator` subset disagree on any schema that uses a
  keyword only the wide one implements. That is intended — they answer
  different questions — but it means a schema written for the contract gate
  is not automatically a schema the output gate fully understands.
