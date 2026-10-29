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
after environment approval. The preparation job never accesses APD.

## Review, then approve

1. Push this branch to the fork. Preparation tests run automatically.
2. Open the Actions run named **APD integration preview**.
3. Inspect its summary for the exact project commit, image digest, profile and
   region. Download **apd-sample-report** and open `report.html` to inspect the
   report format before any live test. Every sample is labeled as sample data.
4. Configure the fresh instance secrets, then select **Review deployments** and
   approve **apd-integration** when you are ready to provision infrastructure.
5. After completion, download **apd-live-report-<run>-<attempt>** and open
   `report.html`. JSON, JUnit, a Markdown summary and a resource ledger accompany it.
   A failed test still produces a report when the runner can finalize normally.

GitHub requires a `workflow_dispatch` workflow to exist on the default branch.
This preview deliberately uses a branch-push trigger so it can run without merging
to your fork's main. Re-run the workflow from its existing run page to repeat the
same candidate commit; a new branch push tests the new commit. If this workflow is
later placed on main, its Run workflow form becomes available too.

The initial candidate is the immutable image digest for
`quay.io/ansible-product-demos/apd-ee-27:20261028` observed during job 25 diagnosis.
Set the repository variable `APD_EE_IMAGE` to select another accessible image before
starting a new run. Preparation resolves tags to digests once, before approval.
The default public image does not need registry credentials in GitHub. Private
image resolution/build credentials are not implemented in this first preview.

Repository variables `APD_TEST_PROFILE` (`baseline-smoke`, `cloud-smoke`, or
`network-smoke`), `APD_TEST_REGION` (`us-east-2` or `us-west-2`), and `APD_ACCESS_CIDR`
control the run. Defaults are baseline smoke, us-east-2, and the APD management
CIDR default. The baseline profile creates five cloud VMs and one c8i.4xlarge
Containerlab hypervisor with four virtual network devices; it can take hours.

## What actually runs

1. Acquire a Controller-side integration lease. An existing lease blocks another
   run until its resources are reconciled, including runs from another entry point.
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

The tests use an in-memory Controller. No credentials or live infrastructure are
used. A local live run uses the same `run.py` CLI as Actions and requires APD
credentials in environment variables plus an immutable image digest and commit SHA.
