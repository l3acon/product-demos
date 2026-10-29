# Try the integration workflow on your fork

The branch `codex/apd-integration-ci` provides a runnable **baseline smoke** slice
of the full dependency design. It configures a disposable APD instance, provisions
fresh cloud/Containerlab fixtures, runs representative APD jobs and produces a
report. OpenShift remains excluded. No upstream PR or Pages deployment is needed.

## Supply a fresh APD instance

The instance needs APD's bootstrap project and setup templates plus working AWS
credentials. A vanilla RHDP APD instance supplies those integrations; an empty AAP
installation does not. The runner preserves opaque machine credentials, fills a
missing SSH key with a new RSA key and a missing Windows password with a generated
test password, and refreshes the AAP callback credential from the approved connection.
It does not manufacture AWS or Red Hat entitlement credentials.

Configure the protected environment from your authenticated GitHub CLI session:

```bash
python -m pip install -r tests/integration/requirements.txt
python tests/integration/configure_fork.py \
  --repo l3acon/product-demos --reviewer l3acon
```

It prompts for **URL, username and password**, validates the URL, creates the
`apd-integration` environment with you as required reviewer, restricts it to this
branch, and uploads three separate encrypted GitHub environment secrets through
stdin. The password is not a workflow input, shell argument, or PR comment.
Alternatively set `APD_URL`, `APD_USERNAME` and `APD_PASSWORD` in that environment's
Secrets settings. Do this before approving the waiting job.

An optional `APD_TOKEN` environment secret replaces password authentication.
Remove stale token secrets when switching back to username/password: the token
takes precedence. Credentials are only made available to the live runner step
after environment approval. The preparation and EE build jobs never access APD.

## Review, then approve

1. Push this branch to the fork. Preparation tests and the EE build run automatically.
2. Open the Actions run named **APD integration preview**.
3. Inspect its summary for the exact project commit, image digest, profile and
   region. Download **apd-ee-build-<run>-<attempt>** for the actual build/runtime
   report and source provenance. **apd-sample-report** previews the integration
   report format with explicitly labeled sample data.
4. Configure the fresh instance secrets, then select **Review deployments** and
   approve **apd-integration** when you are ready to provision infrastructure.
5. After completion, download **apd-live-report-<run>-<attempt>** and open
   `report.html`. JSON, JUnit, a Markdown summary and a resource ledger accompany it.
   A failed test still produces a report when the runner can finalize normally.
   The dashboard highlights the failing phase and links directly to Controller logs.

GitHub requires a `workflow_dispatch` workflow to exist on the default branch.
This preview deliberately uses a branch-push trigger so it can run without merging
to your fork's main. Re-run the workflow from its existing run page to repeat the
same candidate commit; a new branch push tests the new commit. If this workflow is
later placed on main, its Run workflow form becomes available too.

## Build the candidate EE

By default CI builds `execution_environments/apd-ee-27.yml` and
`requirements-27.yml` from the same commit as the APD project. This first build
lane targets **AAP 2.7 on linux/amd64**; other AAP versions require an explicit
externally built image. It runs before APD approval and has no APD credentials.
Build secrets are available only on this fork's trusted branch push/manual path;
there is no privileged pull-request trigger.

Configure these repository secrets once:

| Secret | Purpose |
| --- | --- |
| `REDHAT_REGISTRY_USERNAME` | Red Hat registry account or service account |
| `REDHAT_REGISTRY_PASSWORD` | Pull the definition's base image |
| `ANSIBLE_GALAXY_SERVER_CERTIFIED_TOKEN` | Download certified collections |
| `ANSIBLE_GALAXY_SERVER_VALIDATED_TOKEN` | Download validated collections |

Use the same current [Automation Hub offline token](https://console.redhat.com/ansible/automation-hub/token)
for both Hub secrets, without quotes or a Bearer prefix. Generating a new token
can revoke the previous one. CI validates their format and SSO refresh before
building; only the HTTP status and an allowlisted error code enter the report.

CI resolves the base tag to a digest and builds with that digest. Hub tokens use
required Podman secret mounts in the two Galaxy install commands, rather than
build arguments. Secret sources explicitly use `type=file`: automatic detection
can otherwise select environment mode when CI exports a variable with the same
secret ID, causing the supplied file path to be read as an environment variable name.
Temporary auth/token files live outside the build context and
are removed after the step. Generated build contexts and raw logs are not uploaded.

The built image must pass the same runtime compatibility probe locally before
publishing. Failure produces an HTML/JSON/JUnit build report and blocks APD access.
A passing image is published to
`ghcr.io/l3acon/product-demos/apd-ee-27:ci-<commit>-<run>-<attempt>` using the
workflow's `GITHUB_TOKEN`; no registry push secret or public package setting is
required. The protected APD job receives only the resulting digest.

For private GHCR pulls, the live job creates a run-owned Controller Container
Registry credential using its own temporary `GITHUB_TOKEN` with `packages: read`.
It attaches that credential to Product Demos EE, reapplies the binding after setup,
restores the previous binding and deletes the temporary credential during cleanup.
The token expires with the live job and is never included in reports or the ledger.

`build-metadata.json` records the source commit, image/base digests, definition,
requirements/config/Containerfile checksums, builder version, RPM URL and platform.
The live runner validates that this evidence matches its selected commit and image,
includes it in the final report, and repeats runtime validation inside APD.

The integration workflow reads the immutable image directly from the matching
metadata artifact produced by its preceding build job. Image names do not pass
through job outputs, so registry usernames masked as GitHub secrets cannot erase
the selected image. Public image overrides and the
`APD_EE_IMAGE` workflow variable are removed. A required provenance check rejects
external/Quay images before Controller changes. The Controller runtime probe must
match the build probe's exact collection, core and Python package versions, and
runs again after category setup before AWS checks.

Setup templates retain the pinned `apd_ee_image`, project remote and revision in
their defaults, including Single demo setup jobs launched by Multi-demo setup.
Bindings and project/organization defaults are reapplied before test launches.
Local diagnostic commands can still inspect an external image explicitly; the
approved CI path always tests the image from its preceding GHCR build.

### Initial image finding

The first approved run correctly stopped at EE validation. A local amd64 execution
of the same pinned digest reproduced these findings on October 29, 2026:

| Component | Installed | Candidate source requirement / result |
| --- | --- | --- |
| Ansible core | 2.16.19 | Four installed collections below require core >=2.18 |
| community.general | 13.5.0 | Requires core >=2.18; source requires <12 |
| chocolatey.chocolatey | 1.6.0 | Requires core >=2.18; source requires <1.6 |
| amazon.aws | 12.0.0 | Requires core >=2.18; source requires <10 |
| community.aws | 12.0.0 | Requires core >=2.18; source requires <10 |
| arista.eos | Absent | Source requires 12.3.0 |
| juniper.device | Absent | Source requires 2.0.1 |
| passlib | Absent | EE definition requires >=1.7.4 |

This finding motivated the default source build stage. That old digest still fails compatibility validation; CI now builds and tests
the GHCR candidate without a Quay fallback.

Repository variables `APD_TEST_PROFILE` (`baseline-smoke`, `cloud-smoke`, or
`network-smoke`), `APD_TEST_REGION` (`us-east-2` or `us-west-2`), and `APD_ACCESS_CIDR`
control the run. Defaults are baseline smoke, us-east-2, and the APD management
CIDR default. The baseline profile creates five cloud VMs and one c8i.4xlarge
Containerlab hypervisor with four virtual network devices; it can take hours.

## What actually runs

1. Acquire a Controller-side integration lease. A prior completed run that stopped
   before provisioning can be reconciled automatically using its verified GitHub
   artifacts. Other existing leases block the run until manually reconciled.
2. Capture the source project/EE/binding configuration. Set **Product Demos EE** to
   the candidate digest with pull policy `always`. Point **Ansible Product Demos**
   at this fork and exact commit; sync and verify the revision.
3. Run the local diagnostic playbook in the candidate EE. Verify real Ansible
   module execution, installed collections, their core requirements and the
   candidate source version constraints. Record architecture, versions and tools.
   An incompatible image stops before infrastructure provisioning and is reported.
4. Run **APD / Single demo setup** for Cloud, then **APD / Multi-demo setup** for
   Cloud, Infrastructure, Linux, Network and Windows. Satellite setup is externally
   gated and omitted from this first executable profile. Refresh the existing AAP
   callback credential from the approved connection so setup and network callbacks
   use the supplied instance URL/authentication; never export those values.
5. Rebind ordinary templates, including explicit legacy EE overrides, and eligible
   inventory sources to the candidate. AO and ROSA keep their specialized runtime
   declarations and are not executed by this profile.
6. Query AWS using the instance's existing AWS credential. Reserved VPCs,
   keypairs or instance names must be absent. Provisioning refuses an existing
   stack rather than adopting or deleting it.
7. Launch **Deploy Cloud Stack in AWS**, observe its child jobs, explicitly sync
   AWS inventory, require all five hosts, then run **LINUX / Fact Scan** and
   **WINDOWS / Test Connectivity** using the candidate EE.
8. Launch **NETWORK / Deploy Containerlab Stack**, observe its child jobs and sync
   inventory. Run Configure Devices, Validate Devices, Troubleshoot, Backup and
   Report in order. Network validation does not require RHSM/Insights credentials.
9. Attempt cleanup after success or failure, including partial provisioning.
   Validate VPC and instance ownership and recorded new keypair IDs before deletion;
   the source provisioner does not tag `clab-key` with an owner. Run both destroy
   workflows as applicable, remove the unique report bucket and verify fixture
   absence. Restore captured bindings/project source and its checkout, then remove
   temporary diagnostics and release the integration lease.

The runner uses the actual setup and demo templates from the candidate revision.
It overrides VPC Report's bucket name with a unique run bucket so the test does not
publish to a preexisting account-wide report bucket. Category-created APD objects
and newly generated Containerlab credentials remain configured on the disposable
instance, as do initialized machine credentials and the refreshed AAP callback credential; this is not a transactional
rollback of all category setup changes.

## Interpreting the first report

- Runtime diagnostics and supporting updates appear separately from APD template
  coverage. A successful workflow is expanded to show reached and untaken nodes.
- A job with no target hosts fails the smoke check instead of counting as coverage.
  Errors in child jobs remain visible even if the parent workflow reports success.
- EE evidence verifies the immutable image configured on the job's EE record.
  Actual Kubernetes pod digest evidence is explicitly unavailable through this
  Controller-only preview; it is not claimed as independently observed.
- The runner waits for terminal-job event ingestion and recovers runtime metadata
  from structured `set_stats` or probe-command events when `job.artifacts` is empty.
  Failed collections, Python packages and tools appear in the report and log summary.
- The Controller runtime assertion also names failed collections, missing Python
  packages and failing tools. Compare the job ID with the report's runtime case;
  the broad **Configure baseline** Actions step also includes later API updates.
- AAP callback credential updates read the installed credential type's input
  schema. A string timeout is sent as `"10"`, numeric schemas receive `10`, and
  types without that field omit it. This avoids sending an integer to AAP's string
  field. Callback failures have a separate report case; HTTP 400 diagnostics expose
  recognized field IDs only, never rejected values or raw server validation text.
- JSON/JUnit/HTML contain only selected metadata and normalized failure categories.
  Raw stdout, exception bodies, credential inputs and environment dumps are not
  uploaded. Inspect job IDs in Controller when detailed debugging is needed.
- Not-selected catalog cases remain visible. This is a working first slice of the
  116-template design, not full catalog, patching, hardening or upgrade acceptance.

## Interrupted runs and cleanup failures

The runner checkpoints reports and `ledger.json` while it works. On a normal
failure or timeout it cancels active jobs before deleting resources. It refuses
cleanup if ownership cannot be verified or jobs cannot be stopped. A cleanup or
configuration restoration failure retains the integration lease and fails the run.

Controller inventory deletion returns an empty HTTP 202 while removal is queued.
The client accepts that response and polls until the resource is absent before
releasing the lease. The original preview incorrectly treated this as a JSON error.

On retry, automatic recovery is limited to a completed integration workflow in the
same repository with matching commit/run/owner evidence, explicit false provisioning
flags, no active jobs, and a verified prior configuration restore. It validates
the entire resource list and current object ownership before deleting only those
temporary inventories/templates/registry credentials and the stale lease. It never deletes AWS resources
or repairs unknown configuration during this recovery. Read access to Actions
artifacts is supplied by the live job's read-only `GH_TOKEN`.

Runner loss or forced cancellation cannot guarantee cleanup. Inspect the ledger,
the Controller job IDs and the lease's run owner before manually cleaning the
reserved AWS fixtures and restoring project/EE configuration. The public ledger
contains resource IDs and changed field names, never original secret-bearing
configuration values. Automatic recovery after a lost runner is not implemented
yet. Do not remove the lease until reconciliation is complete. Start with a fresh
instance for the next approved run if recovery is uncertain.

## Local validation

```bash
python tests/integration/build_graph.py
python -m unittest discover -s tests/integration/tests -v
python tests/integration/report.py --sample --output integration-results/sample
```

The tests use an in-memory Controller and simulated build commands. No credentials
or live infrastructure are used. A local live run uses the same `run.py` CLI as Actions and requires APD
credentials in environment variables plus an immutable image digest and commit SHA.

## Read the dashboard and diagnostics

The HTML report starts with the overall result, check counts and a highlighted
failure panel. A phase timeline separates APD configuration, EE validation, category
setup, AWS baseline, cloud tests, network tests and cleanup. Failed checks open by
default. The image card shows the exact GHCR digest and AWS collection versions;
the collection inventory compares the CI build with Controller observations.

Job links open authenticated Controller output. `run.log` contains a sanitized
execution trace and canonical task/module/source locations. Raw stdout, invocation
arguments and exception bodies are never copied. Rendered task names are replaced
by names read from the candidate source, and unknown hosts are omitted. The
Controller origin appears in log links; login credentials do not.

AWS fixture queries keep `no_log` protection. Their rescue path publishes only a
known module name and an allowlisted failure category. A failed query is reported
as an inspection failure, rather than incorrectly claiming existing resources.
An actual non-empty baseline remains a refusal to provision. Cleanup failures are
highlighted even when all test jobs passed.

## Publish the tested image to Quay

A second workflow, **Approve tested EE publication to Quay**, is called after the
APD test completes. It works from this preview branch as a reusable workflow,
before any merge or PR. Its standalone manual form becomes available when the
workflow is on the default branch, as required by GitHub.

The unprivileged planning job downloads the completed integration attempt's report,
validates the workflow/commit/image provenance and successful EE runtime checks,
and records the exact source digest, destination tag, test outcome and cleanup
status in **apd-quay-plan-<run>-<attempt>**. It refuses samples, external images,
incomplete tests and failed runtime validation. Cloud/network smoke failures may
still be explicitly accepted by the reviewer; the summary displays those failures
before approval. This is publication of a tested candidate, not full-catalog certification.

The publishing job requires a separate **quay-publish** environment approval. The
fork environment is configured with `l3acon` as required reviewer, administrator
bypass disabled and this branch allowed. Configure **QUAY_USERNAME** and
**QUAY_PASSWORD** in that environment (a Quay robot account with write access is
suitable). The caller also supports the two named repository secrets when already
configured there; only the protected publish job maps them into its process.
Environment secrets take precedence. APD approval does not approve registry publication.

The default destination is
`quay.io/ansible-product-demos/apd-ee-27:tested-<commit>-<source-run>-<attempt>`.
Repository variables **APD_QUAY_REPOSITORY** and **APD_QUAY_TAG** can choose another
destination or an explicit tag before a run. `latest` is never changed implicitly;
selecting it explicitly makes it visible in the approval plan. The standalone form
also accepts repository/tag inputs when available.

After approval, `skopeo copy --all --preserve-digests` copies the immutable GHCR
source without rebuilding. The job verifies the returned digest and the final
Quay tag independently, then saves **apd-quay-published-<run>-<attempt>** with the
source, destination, digest and original test outcome. No Quay write occurs while
the approval is pending.

The AWS instance tasks use `placement.tenancy`, as supported by amazon.aws 9.5.2,
rather than the deprecated top-level `tenancy` argument, which is removed in
newer AWS collection releases. Unsupported-argument diagnostics
retain only module/parameter names present in the candidate source; invocation
values and AWS credential fields are never copied to the report.
