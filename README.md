# WorkVault

**Escrow secured by GenLayer validator consensus.**

WorkVault is a freelance/bounty escrow dApp built on [GenLayer](https://genlayer.com). Instead of a human arbitrator or an off-chain dispute process deciding whether a deliverable meets the brief, WorkVault uses GenLayer's Intelligent Contracts to have validators independently judge the submission against stated acceptance criteria — and reach consensus on-chain.

🔗 **Live app:** [kashif130.github.io/workvault](https://kashif130.github.io/workvault/)

---

## Why

"Did the freelancer actually deliver what was agreed?" is normally a manual, trust-based, or off-chain-arbitrated question. It can't be checked with a simple deterministic rule (a file exists, a hash matches) — it needs actual reading and judgment: does this report cover the brief, does this code satisfy the spec, was this design delivered as described.

WorkVault turns that judgment call into an on-chain decision made by GenLayer validator consensus, using a **non-comparative equivalence principle**: validators don't need to produce identical wording, they just need to independently reach the same verdict category given the same brief and criteria.

## How it works

1. **Fund an escrow** — the payer deposits funds, names a payee, and writes a brief (what's being delivered) and acceptance criteria (how it should be judged).
2. **Submit a deliverable** — the payee submits proof of work: a description and/or a link to the actual deliverable.
3. **Verify** — anyone can trigger verification. Validators independently review the brief, criteria, and submission via an LLM prompt, and return a verdict: `APPROVED`, `REJECTED`, or `NEEDS_REVISION`, plus reasoning.
4. **Settle**
   - If `APPROVED` → the payee can **withdraw** funds.
   - If `REJECTED` / `NEEDS_REVISION` → the payee can revise and resubmit, or — if the payer enabled refunds at creation — the payer can **reclaim** funds at any point before approval.

```
FUNDED → SUBMITTED → APPROVED → RELEASED
            ↑            ↓
            └── REJECTED ┘
                  ↓ (if refund_enabled)
               REFUNDED
```

This keeps "judge the work" (LLM consensus, re-runnable) separate from "move the money" (a single, guarded withdraw path) — the standard escrow safety pattern, applied to a subjective/judged deliverable instead of a deterministic condition.

## Repo contents

| File | Description |
|---|---|
| `contracts/deliverable_escrow.py` | The GenLayer Intelligent Contract — `DeliverableEscrow` |
| `index.html` | Single-page frontend: Browse / Post a Job / My Escrows / Escrow detail |

## The Intelligent Contract

Built with [`genlayer`](https://docs.genlayer.com) (py-genlayer), targeting GenVM.

**State per escrow:** payer, payee, amount, brief, criteria, submission, status, verdict reasoning, submit count, refund flag, and the last raw validator response (for debugging parse issues).

**Public methods**

| Method | Caller | Description |
|---|---|---|
| `create_escrow(payee, brief, criteria, refund_enabled)` | payer | Payable — funds a new escrow |
| `submit_deliverable(escrow_id, submission)` | payee | Submits/resubmits proof of work |
| `verify_deliverable(escrow_id)` | anyone | Triggers validator consensus judgment |
| `withdraw(escrow_id)` | payee | Claims funds after `APPROVED` |
| `refund(escrow_id)` | payer | Reclaims funds if never approved (requires `refund_enabled`) |
| `get_status` / `get_amount` / `get_submission` / `get_verdict_reasoning` / `get_brief` / `get_criteria` / `get_payer` / `get_payee` / `get_refund_enabled` / `get_last_raw_response` / `escrow_count` | — | Views |

**Consensus step:** `verify_deliverable` calls `gl.eq_principle.prompt_non_comparative`, which prompts each validator with the brief, criteria, and submission, and asks for a two-line response (verdict word + one-sentence reasoning). Equivalence between validators is judged only on verdict category, not exact wording. The raw response is parsed defensively by keyword scan (not strict JSON), since the pinned model doesn't reliably return structured output.

## The frontend

A single-file vanilla HTML/CSS/JS dApp — no build step.

- **Browse** — lists all escrows on the contract.
- **Post a Job** — form to fund a new escrow (payee, brief, criteria, amount, refund toggle).
- **My Escrows** — escrows where the connected wallet is the payee.
- **Detail view** — shows full escrow state, and conditionally shows action cards (submit / verify / withdraw / refund) based on the connected wallet's role and the escrow's current status.

## Running locally

The frontend is static — no build tooling required.

```bash
git clone https://github.com/kashif130/workvault.git
cd workvault
# serve index.html with any static server, e.g.:
python3 -m http.server 8080
```

Then open `http://localhost:8080` and connect a wallet configured for the GenLayer network the contract is deployed to.

Deploying the contract itself requires the GenLayer CLI/Studio — see the [GenLayer docs](https://docs.genlayer.com) for deployment steps.

## License

MIT
