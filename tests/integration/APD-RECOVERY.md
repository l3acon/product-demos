# Reuse an APD instance after a failed CI run

Manual recovery has two operations: **plan** and **apply**. It keeps the APD base
project, demo templates, existing AWS/machine/AAP credentials and job history.
It removes verified run-owned Cloud Stack / Containerlab infrastructure, temporary
CI objects, owned inventory hosts and finally the old integration lock. No APD
reprovisioning or candidate EE rebuild is required.

The source is the run that owns the retained lock, not necessarily the latest
failed action. For the current instance that is `41083465329-2`; run `41100897020-1`
failed before creating anything. Recovery accepts a finished APD job even when the
whole GitHub workflow is still waiting on optional Quay publication.

## Use the existing GitHub environment secrets

From a clean `codex/apd-integration-ci` checkout:

```bash
python tests/integration/recover_apd.py trigger \
  --mode plan --source 41083465329-2 --region us-east-2
```

The helper pushes an empty request commit on the existing fork branch. This makes
manual recovery usable before a PR merges the workflow onto the default branch.
The normal integration workflow skips these request commits. A separate **Manual
APD recovery** action requests the existing `apd-integration` environment approval,
then uses its saved credentials. It does not publish an image or run the catalog.

Review `apd-recovery-plan-<recovery-run>-<attempt>`: `report.html` and
`recovery-plan.json` list AWS account/region, IDs, ownership, planned job
cancellations, Controller objects, untagged keys and any blockers. Planning creates
and then removes its own isolated EE/project/inventory/template; it does not change
the Product Demos EE or project source and does not delete previous-run resources.

After authorizing the reviewed plan:

```bash
python tests/integration/recover_apd.py trigger \
  --mode apply --source <recovery-plan-run>-<attempt>
```

For an old untagged Containerlab keypair, include the exact reviewed ID:

```bash
python tests/integration/recover_apd.py trigger \
  --mode apply --source <recovery-plan-run>-<attempt> \
  --adopt-key key-0123456789abcdef0
```

Apply has the same protected environment approval. It downloads the immutable plan
artifact from a completed recovery action, revalidates source/instance/owner and
resource IDs, stops the reviewed APD jobs and waits for their descendants/events.
It deletes supported resources by ID, verifies absence, removes only ledger-owned
Controller objects and releases the old lock last. New or foreign resources block
apply. A failed partial cleanup keeps the old lock and produces a receipt; retry is
allowed only within the reviewed resource set. Old recovery locks from a completed
recovery action can be reconciled after its utility jobs have settled.
Utility cleanup failures retain that recovery lock and its object ledger for retry.
The requested region must match source metadata and recorded fixture launch
variables. Legacy reports without a region require launch-variable evidence for
each provisioned fixture; missing or conflicting evidence blocks recovery.

When the workflow reaches the default branch, its normal **Run workflow** form
provides the same plan/apply operations without a request commit. Select a branch
allowed by the environment policy (currently `codex/apd-integration-ci`).

## Run locally

With APD credentials already configured as `APD_URL`, `APD_TOKEN` or
`APD_USERNAME`/`APD_PASSWORD`, plus `gh` authentication:

```bash
python tests/integration/recover_apd.py plan \
  --source 41083465329-2 --region us-east-2 \
  --revision "$(git rev-parse HEAD)"

python tests/integration/recover_apd.py apply \
  --plan integration-results/recovery/recovery-plan.json \
  --revision "$(git rev-parse HEAD)"
```

Private GHCR images need `APD_REGISTRY_USERNAME` and `APD_REGISTRY_PASSWORD` in the
local environment. The GitHub workflow supplies its temporary job token itself.
Credentials are never copied into reports, plans, receipts or command-line values.

## Scope and failure behavior

AWS ownership is the source run's unique `apd-ci-<id>` tag, with a separate explicit
adoption step for exact untagged reserved key IDs. A plan covers EC2 instances,
their owned VPC networking, tagged keypairs and the exact unique report bucket.
Unrelated instances inside an owned VPC, conflicting reserved names and unsupported
NAT/endpoint/peering/transit dependencies are blockers. Snapshot/restore runs and
unfinished advanced-network cleanup require their additional adapters; recovery
reports this instead of silently declaring those resources clean.

The utility runs Python from its isolated EE, using the existing APD AWS credential.
It does not depend on the failing demo teardown templates or on Controller's
system Python interpreter. Permission/runtime errors produce a failed report and
keep the source lock. Base APD objects and unrelated AWS resources are preserved.

Review `apd-recovery-result-<run>-<attempt>` for `ready_for_test: true`. Then rerun
the failed integration action to reuse its successful EE build; approve APD access
again. If the cleanup utility itself was terminated, inspect the recorded utility
job IDs before attempting another recovery. Do not delete an active recovery lock.
