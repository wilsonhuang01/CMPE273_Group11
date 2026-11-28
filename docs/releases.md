# Image releases and staged promotion (action S)

A release is one tagged source revision built into two images and promoted by
digest. Nothing in this flow uses stored AWS access keys: GitHub Actions gets
short-lived credentials through OIDC.

| Image | Contents | Platforms |
|---|---|---|
| `cmpe273-optimizer/app` | coordinator, scheduler, worker and agent services ([Dockerfile](../Dockerfile)) | linux/amd64, linux/arm64 |
| `cmpe273-optimizer/compiler` | trusted sandbox toolchain ([compiler.Dockerfile](../docker/compiler.Dockerfile)); each platform labels its own target | linux/amd64 (`linux-x86_64`), linux/arm64 (`linux-aarch64`) |

## What a tag does

Pushing `vX.Y.Z` runs [release.yml](../.github/workflows/release.yml):

1. **Gates.** All of [CI](../.github/workflows/ci.yml) runs as a reusable workflow:
   unit, PostgreSQL, managed-engine, Compose, Docker integration, clean-release and
   Terraform checks. `release.py check-tag` requires the tag to equal the
   `pyproject.toml` version, every SQL migration shipped by the previous release to
   be byte-identical, and the schema version to be no lower.
2. **Native builds.** Each platform builds on its own runner (`ubuntu-24.04` and
   `ubuntu-24.04-arm`, or the `ARM64_RUNNER` variable). The job fails if the runner's
   machine does not match, so a release image is never a QEMU build. Before pushing,
   the job runs the Docker baseline and measurement-tool tests against the candidate
   compiler image on that hardware. Images carry OCI version/revision labels and
   `compiler-optimizer.build-mode`.
3. **Publish.** Per-platform tags (`vX.Y.Z-amd64`, `vX.Y.Z-arm64`) are pushed to
   immutable repositories, then a `vX.Y.Z` multi-arch index is created.
   Per-platform tags keep the ECR lifecycle policy from expiring images an index
   still references.
4. **Manifest.** `release-manifest.json` (a workflow artifact for 90 days) records
   the revision, every digest, base images, the Clang version, the
   constraints/pyproject/contract hashes, the database schema version, the build
   mode and native validation for each platform, and scan results.
5. **Scan gate.** The job waits for ECR push-time scans. Any CRITICAL finding that
   is not in [scan-allowlist.json](../infra/aws/release/scan-allowlist.json) (each
   entry needs a reason and an expiry date) blocks promotion.
6. **Staging.** The `deploy-staging` job runs in the `staging` GitHub environment,
   so its protection rules and reviewers apply. `release.py promote` writes the
   release pointer, then the staging checks run. If a check fails after the
   pointer moved, the job rolls staging back. A refused promotion changes nothing,
   so nothing is rolled back.

## The release pointer and rollback

Each environment's deployed release is the SSM parameter
`/cmpe273-optimizer/<env>/release`: version, revision, schema version, contract hash
and the two index digests. Action T's ECS services and action U's workers will run
exactly these digests. The parameter history is the rollback list.

Promotion is refused when:

- either image lacks a natively built and validated image for the environment's
  `worker_architecture` (staging and production are arm64);
- any image is unscanned or has unaccepted findings;
- the release supports a lower database schema than the environment has applied.

Migrations run forward at startup, and older builds refuse schemas they don't know.
So `rollback` picks the newest earlier release whose schema is at least the highest
schema any release in that environment applied. If that rules out the release you
want, it refuses and tells you to fix forward with a new release.

```sh
./project release status --env staging              # history, newest last
./project release rollback --env staging            # previous compatible release
./project release rollback --env staging --to 0.1.0
```

The same rollback runs from GitHub: **Actions → Release → Run workflow**.

## One-time setup

Requires the [AWS foundation](aws-infrastructure.md) state bucket (`./project infra bootstrap`).

```sh
./project infra release-plan     # review var/infra/release-apply.txt
./project infra release-up       # ECR repositories and GitHub OIDC roles
./project infra release-output   # values for GitHub
```

Edit [release.tfvars.json](../infra/aws/config/release.tfvars.json) first if the
repository is not `wilsonhuang01/CMPE273_Group11`. If the account already has a GitHub
OIDC provider, set `create_github_oidc_provider` to `false`. In GitHub, set these
**repository variables** (not secrets): `AWS_REGION`, `AWS_RELEASE_ROLE_ARN` and
`ECR_REGISTRY`. Then create the `staging` **environment** with `AWS_DEPLOY_ROLE_ARN`
set to `deployer_role_arns.staging`, and add required reviewers if promotion should
wait for approval. arm64 runners `ubuntu-24.04-arm` are free for public repositories.
Private repositories need a larger-runner label in `ARM64_RUNNER`.

To cut a release, bump `version` in `pyproject.toml`, merge, then:

```sh
git tag v0.2.0 && git push origin v0.2.0
```

## Current limits

- Not yet run against an AWS account: the release stack hasn't been applied and no
  tag has been released.
- The staging checks confirm that the pointer names the published digests. Rolling
  ECS services to those digests and waiting for health is action T, and worker
  image pull is action U. Until then, "deployed to staging" means the pointer moved.
- Only CRITICAL findings block. Debian/Ubuntu base images usually report HIGH and
  lower findings, and these are recorded in the manifest.
- Model adapters keep their deterministic and replay tests in the CI gate. Release
  builds never call a live model.
