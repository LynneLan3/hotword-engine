# Batch Receipt V1

`jobs/batches/<batch_id>/batch_receipt.json` is the shared trace for one
automatic research batch. `batch_id` must remain unchanged across research,
implementation, deployment, and indexing updates.

Lifecycle states are:

`RESEARCH_PENDING → RESEARCH_PASS → IMPLEMENTATION_PENDING → IMPLEMENTED → DEPLOYMENT_PENDING → DEPLOYED → INDEXING_CHECKED`

Any stage may end in `FAILED`; a failed receipt is terminal. The research
scheduler writes the initial receipt and can include the same `batch_id` in the
Research Job callback. It never claims implementation or production evidence.

An implementation/deployment owner can apply an evidence patch with:

```text
python3 update_batch_receipt.py \
  --receipt jobs/batches/<batch_id>/batch_receipt.json \
  --patch implementation-or-deployment-patch.json
```

`IMPLEMENTED` requires action (`UPDATE`, `NEW`, or `EXPAND`), changed files,
and canonical URLs. `DEPLOYED` additionally requires commit SHA/URL,
production URL, and changed URLs. `INDEXING_CHECKED` requires at least one
indexing result. These checks prevent a research result from being displayed
as a deployed content batch.

## Ownership and next hop

- Research queue and Research Job state: GSC Research Tasks; execution and
  callback transport: `hotword-engine`.
- Implementation orchestration: `hotword-control-center` cloud auto-content
  operator (`scripts/cloud-auto-content-operator.mjs`).
- Site implementation and Vercel deployment: the target game repository and
  its existing `publish:production` contract.
- Production Intervention Receipt and indexing writeback: GSC runtime.

The existing callback updates Research Job state but does not yet deliver a
receipt patch back into this repository. The next legal integration is for the
cloud operator to carry this `batch_id` into its handoff/publisher result and
invoke the receipt patch contract; no game-site repository is modified here.
