# ADR-0026: The cold lake's blob substrate is S3, not MinIO

## Status

Accepted

## Context

ADR-0004 replaced MinIO with a content-addressed filesystem store, because the target
laptop could not run MinIO and its footprint was large. ADR-0019 restored the server
topology, and the cold lake's blob substrate with it: on the server, an oversized
signal's payload is offloaded to an S3 object rather than kept in the database.

The substrate was named `minio` throughout — the configuration key was
`backend_coldlake="minio"`, the settings were `minio_endpoint`, `minio_bucket`,
`minio_secure`, `minio_access_key_env` and `minio_secret_key_env`, the optional
dependency was `paa[minio]`, and the module was
`storage/coldlake/minio_backend.py`.

That name stopped being true. The MinIO community distribution is archived upstream:
the download endpoint answers `410 Gone` with a notice that the project is no longer
maintained, and the container images cannot be pulled anonymously from either Docker
Hub or `quay.io` — the `quay.io/minio/minio` path answers `401` for an anonymous
pull. The `aistor` sidecar image does pull, but it requires a Kubernetes API to talk
to and is not usable standalone. There is no supported way to run the substrate the
code was written against.

Two things were also true of the code as it stood. The endpoint was not configurable
in the sense that mattered: the substrate was addressed as *MinIO*, so the only
servers that could be pointed at were MinIO servers. And the failure modes that
matter for a blob store — a server restarted mid-upload, two writers racing for one
key, a large multipart object, a full disk — were not exercised anywhere.

## Decision

**The substrate is S3, and the server is whatever speaks it.**

- **The name is the protocol, not the vendor.** `backend_coldlake` accepts `"s3"`, and
  the settings are `s3_endpoint`, `s3_bucket`, `s3_secure`, `s3_access_key_env` and
  `s3_secret_key_env`. The endpoint is configurable, which is the point: the same
  `S3BlobStore` runs against SeaweedFS in the Docker deployment and against RustFS in
  a CI environment, and the test suite runs against both.
- **The deployment uses SeaweedFS.** Not because it is better than the alternatives —
  it is one of several — but because the alternatives are not runnable: MinIO's images
  cannot be pulled, and the fork the ecosystem moved to is a fork.
- **The names that shipped keep working.** A deployment written against the earlier
  names keeps working without editing its `.env`: `backend_coldlake="minio"` is
  accepted, and the `PAA_STORAGE__MINIO_*` environment variables are accepted through
  a `model_validator(mode="before")` on `StorageSettings`. When both are set, the new
  name wins. This is the same rule the configuration already used for its other
  renames, and the reason for it is the same: an operator's deployment should not
  break because a key was renamed.
- **The optional dependency is renamed to `s3`.** `paa[s3]` installs `boto3`, which
  speaks the S3 API. `paa[minio]` is removed rather than kept as an alias: pip
  warns about a missing extra and installs nothing, so an alias would produce a
  runtime failure at first use instead of an installation-time failure, and the
  failure would be about the S3 substrate, which is the honest subject.

## Consequences

**What this costs.**

- `pip install 'paa[minio]'` no longer installs anything. The extra is renamed, and
  the error message the runtime prints when `boto3` is missing now says `paa[s3]`.
  This is a deliberate break: the alternative is an alias that pip treats as a
  warning and that fails later, in a place where the message would have to explain
  two names for one thing.
- The module is still called `minio_backend.py` and the class is `S3BlobStore`. The
  file name lags the decision; renaming it is a mechanical change that touches the
  import sites and the test file, and it is not worth doing in the same change as the
  behaviour it names.
- The two servers the substrate is tested against are not the same server. SeaweedFS
  and RustFS implement S3 with different completeness; the lifecycle that passes on
  both is the lifecycle the runtime depends on, and the differences between them are
  the reason the endpoint is configurable rather than the reason one of them is
  preferred.

**What it would take to move back.** A substrate that is not S3. The repository layer
depends on a `BlobStore` protocol, so the change is confined to one module — which is
the point of having one.
