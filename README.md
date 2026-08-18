# WorkVault

Escrow that releases funds only when GenLayer validators independently agree a submitted deliverable satisfies the acceptance criteria the payer wrote down — not a manual "approve/reject" button on trust.

**Live app:** https://kashif130.github.io/workvault/
**Contract (GenLayer Studionet):** `0x50b8B474762c2ab941B6C132e1dcC08672E375D5`
**Contract source:** https://github.com/Kashif130/workvault/tree/main/contracts

---

## What problem this solves

Freelance/bounty escrow is usually one of two bad options:

- **A human "release funds" button** — the payer just has to trust the payee's word, or a platform arbitrator has to manually read every submission.
- **A fully deterministic on-chain check** — works for "does this hash match," not for "is this actually a good 500-word blog post about X."

WorkVault uses GenLayer's non-deterministic validator consensus instead. The payer writes acceptance criteria in plain English (topic, minimum word count, must be original, etc.). When the payee submits a deliverable — typically a URL — validators **independently fetch the live page** (`gl.nondet.web.render`) and judge the actual content against the stated criteria, then reconcile their verdicts through an equivalence principle (they don't need identical wording, just the same verdict category). Funds only move on that consensus result.

## Lifecycle

```
FUNDED → SUBMITTED → APPROVED → RELEASED   (payee withdraws)
                    → REJECTED → (resubmit) or → REFUNDED (after delay, if enabled)
         SUBMITTED  → DISPUTED → RELEASED | REFUNDED   (arbiter decides)
FUNDED | SUBMITTED             → CANCELLED             (both parties agree)
```

1. **Payer** creates an escrow: deposits funds, names a payee, writes a `brief` (what's being paid for) and `criteria` (how to judge it), optionally enables a timed refund window.
2. **Payee** submits a deliverable — a URL and/or description.
3. **Anyone** triggers `verify_deliverable`. If the submission contains a URL, each validator fetches it live and judges the real content — not just the payee's description — against the brief and criteria. Verdict: `APPROVED`, `REJECTED`, or `NEEDS_REVISION`, plus a reasoning sentence.
4. **Approved** → payee withdraws (minus platform fee, if any). **Rejected** → payee can revise and resubmit, or payer can refund after the delay (if enabled).
5. Either party can **raise a dispute** at any point after submission, escalating to the contract's arbiter for a manual override — an escape hatch for cases consensus can't cleanly settle.
6. Either party can **propose cancellation**; once both agree, funds return to the payer with no verdict needed.

## Why the verification is trustworthy

- Validators don't trust the payee's typed description alone — they fetch the actual live URL content and judge against that.
- Vague criteria get judged loosely; specific criteria (exact topic, minimum word count, "must be original, not boilerplate") get judged precisely. The create-job form nudges payers to be specific.
- Verdicts are reconciled by a non-comparative equivalence principle across validators, not a single model's opinion.
- Every verdict is required to come with a reasoning sentence (minimum 10 words) — not just a bare APPROVED/REJECTED — so a rejection is always explainable, not a black box.
- `get_last_raw_response` exposes the last unparsed validator output for debugging if a verdict ever looks wrong.

## Frontend features

- **Browse / Post a Job / My Escrows** views.
- **Create escrow**: payee address, brief, acceptance criteria (with guidance on being specific), amount, optional refund window.
- **Submit deliverable**: separate URL field + optional description, so the link is always captured cleanly for the on-chain fetch.
  - **Preview & check link**: best-effort client-side fetch of the URL before submitting, showing word count and a content preview — a convenience check only. The authoritative check always happens on-chain when a validator independently fetches the URL at verification time.
- **Verify**: one click triggers validator consensus; shows the verdict and reasoning once resolved.
- **Withdraw / Refund / Propose cancel / Raise dispute / Resolve dispute** (arbiter-only) actions, shown conditionally based on escrow state and connected wallet role.

## Contract functions

| Category | Function |
|---|---|
| Lifecycle | `create_escrow`, `submit_deliverable`, `verify_deliverable`, `withdraw`, `refund` |
| Dispute / cancel | `raise_dispute`, `resolve_dispute` (arbiter-only), `propose_cancel` |
| Admin | `set_fee_bps`, `set_treasury`, `set_arbiter`, `transfer_ownership`, `set_paused` |
| Views | `get_status`, `get_amount`, `get_brief`, `get_criteria`, `get_submission`, `get_verdict_reasoning`, `get_payer`, `get_payee`, `get_refund_enabled`, `get_created_at`, `get_refund_available_at`, `get_submit_count`, `get_last_raw_response`, `get_fee_bps_at_creation`, `get_payer_cancel_vote`, `get_payee_cancel_vote`, `get_disputed_by`, `get_dispute_reason`, `get_dispute_resolution_note`, `get_fee_bps`, `get_treasury`, `get_owner`, `get_arbiter`, `get_paused`, `escrow_count`, `get_payer_escrow_count` / `get_escrow_id_for_payer_at`, `get_payee_escrow_count` / `get_escrow_id_for_payee_at` |

## Platform fee

Optional, basis-points fee (default 0, hard-capped at 1000 bps / 10%) deducted from the payee's release **only on a successful approved withdraw** — never on refunds, cancellations, or arbiter-ordered refunds. The rate is locked in per-escrow at funding time, so a later fee change never retroactively affects escrows already in flight.

## Timestamps

`created_at` and `refund_available_at` are stored on-chain as **Unix seconds** (via `datetime.datetime.now().timestamp()` in `create_escrow`). The frontend consistently converts these to JS milliseconds (`Number(value) * 1000`) before constructing a `Date` — every place a timestamp is read and displayed follows this same conversion, so refund-availability and creation-time never drift relative to each other.

## Files

| File | Purpose |
|---|---|
| `deliverable_escrow.py` | The Intelligent Contract (GenLayer, Python) — all escrow logic and validator verification. |
| `workvault-index.html` | Single-file frontend — wallet connect, create/browse/manage escrows, deliverable submission with URL preview. |
| `test_deliverable_escrow.py` | Contract test suite covering create, browse/detail reads, submission, verification, withdrawal, and timed refund against the submitted contract. |

## Local testing checklist

1. **Create** an escrow as the payer: fund it, write a specific brief + criteria (topic, min word count, must be original).
2. **Submit** as the payee: paste a real URL, use "Preview & check link" to sanity-check word count/content before submitting on-chain.
3. **Verify**: trigger validator consensus, confirm the verdict and that the reasoning box shows an actual explanation (not just the verdict word repeated).
4. **Withdraw** (if approved) or **resubmit** (if rejected) — confirm state transitions and, if a fee is set, that the payee receives amount minus fee.
5. Optionally exercise **refund** (after the delay), **propose_cancel** (both parties agree), and **raise_dispute → resolve_dispute** (arbiter override) paths.

## Notes

- Deployed on **GenLayer Studionet** — a testnet. Update `CONTRACT_ADDRESS` in `workvault-index.html` after every redeploy.
- The client-side link preview uses a public read-only proxy and is best-effort only; some sites will block it. This never blocks submission and has no bearing on the actual on-chain verdict.
