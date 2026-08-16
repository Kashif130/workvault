# v0.1.0
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
DeliverableEscrow — a reusable Intelligent Contract primitive that holds
funds and releases them only when GenLayer validators independently agree
a submitted deliverable satisfies stated acceptance criteria.

This turns "did the freelancer actually deliver what was agreed" from a
manual, trust-based, or off-chain-arbitrated question into an on-chain
judgment call made by consensus — useful for freelance work, bounties,
milestone-based payments, and any escrow where the deliverable can't be
checked by a simple deterministic rule (a file exists, a hash matches),
but needs actual reading/judgment (does this report cover the brief, does
this code satisfy the spec, was this design delivered as described).

Lifecycle
---------
FUNDED -> SUBMITTED -> (APPROVED -> RELEASED) | (REJECTED -> back to SUBMITTED-eligible or REFUNDED after timeout)

1. Payer creates an escrow: deposits funds, names a payee, states the
   deliverable brief and acceptance criteria.
2. Payee submits proof of work (a description and/or a URL to the actual
   deliverable).
3. Anyone can trigger verification. Validators independently review the
   submission against the criteria and return a structured verdict
   (APPROVED / REJECTED / NEEDS_REVISION + reasoning). This is the
   non-deterministic step, reconciled with a non-comparative equivalence
   principle: validators don't need identical wording, they need to
   independently reach the same judgment given the same criteria.
4. If APPROVED, the payee can withdraw the funds — no further action
   needed. If REJECTED or NEEDS_REVISION, the payee can revise and
   resubmit, or the payer can wait out the refund window and reclaim
   funds if the payee never delivers.

This separates "judge the work" (LLM consensus, can be re-run) from
"move the money" (a single, guarded withdraw path), which is the standard
escrow safety pattern applied to a subjective/judged deliverable instead
of a deterministic condition.
"""

from genlayer import *
from dataclasses import dataclass
import json


# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------
STATUS_FUNDED = 0        # payer deposited, waiting on payee to submit
STATUS_SUBMITTED = 1     # payee submitted, waiting on verification
STATUS_APPROVED = 2      # validators approved, payee can withdraw
STATUS_REJECTED = 3      # validators rejected; payee may resubmit
STATUS_RELEASED = 4      # funds withdrawn by payee — terminal
STATUS_REFUNDED = 5      # funds returned to payer after timeout — terminal

ZERO_ADDRESS = Address("0x0000000000000000000000000000000000000000")


@allow_storage
@dataclass
class Escrow:
    payer: Address
    payee: Address
    amount: u256
    brief: str                # what the deliverable should be
    criteria: str              # how validators should judge it
    submission: str             # payee's proof-of-work text/URL, empty until submitted
    status: u256
    verdict_reasoning: str
    submit_count: u256          # how many times the payee has submitted
    refund_after: u256          # unix timestamp payer can refund after, if payee never delivers
    created_at: u256


class DeliverableEscrow(gl.Contract):
    escrows: TreeMap[u256, Escrow]
    next_id: u256

    def __init__(self):
        self.next_id = u256(0)

    # -----------------------------------------------------------------
    # 1. Fund an escrow
    # -----------------------------------------------------------------
    @gl.public.write.payable
    def create_escrow(
        self,
        payee: str,
        brief: str,
        criteria: str,
        refund_after: int,
    ) -> None:
        """
        Deposit funds into a new escrow.

        payee:         address (as a hex string) that will submit the
                        deliverable and receive funds on approval.
        brief:         what is being paid for, e.g. "A 500-word blog post
                        about GenLayer's consensus model."
        criteria:      how validators should judge the submission, e.g.
                        "Approve if the linked post is at least 400 words,
                        specifically discusses GenLayer, and is not
                        plagiarized boilerplate."
        refund_after:  unix timestamp after which the payer may reclaim
                        funds if the escrow is not APPROVED/RELEASED yet.
                        Use 0 to disable the refund path entirely.
        """
        if gl.message.value <= 0:
            raise Exception("escrow must be funded with a positive amount")
        if len(brief.strip()) == 0:
            raise Exception("brief cannot be empty")
        if len(criteria.strip()) == 0:
            raise Exception("acceptance criteria cannot be empty")

        payee_addr = Address(payee)
        if payee_addr == gl.message.sender_address:
            raise Exception("payee cannot be the same as payer")

        eid = self.next_id
        self.next_id = u256(int(self.next_id) + 1)

        e = Escrow(
            payer=gl.message.sender_address,
            payee=payee_addr,
            amount=u256(int(gl.message.value)),
            brief=brief,
            criteria=criteria,
            submission="",
            status=u256(STATUS_FUNDED),
            verdict_reasoning="",
            submit_count=u256(0),
            refund_after=u256(max(refund_after, 0)),
            created_at=u256(0),
        )
        self.escrows[eid] = e

    # -----------------------------------------------------------------
    # 2. Payee submits proof of work
    # -----------------------------------------------------------------
    @gl.public.write
    def submit_deliverable(self, escrow_id: int, submission: str) -> None:
        """
        Payee submits their proof of work: a description, and/or a URL
        pointing at the actual deliverable, for validators to review.
        Callable again after a REJECTED verdict to resubmit revised work.
        """
        eid = u256(escrow_id)
        e = self.escrows.get(eid, None)
        if e is None:
            raise Exception("unknown escrow_id")
        if gl.message.sender_address != e.payee:
            raise Exception("only the designated payee can submit")
        if e.status not in (u256(STATUS_FUNDED), u256(STATUS_REJECTED)):
            raise Exception("escrow is not in a state that accepts submissions")
        if len(submission.strip()) == 0:
            raise Exception("submission cannot be empty")

        e.submission = submission
        e.status = u256(STATUS_SUBMITTED)
        e.submit_count = u256(int(e.submit_count) + 1)
        self.escrows[eid] = e

    # -----------------------------------------------------------------
    # 3. Verify — the non-deterministic consensus step
    # -----------------------------------------------------------------
    @gl.public.write
    def verify_deliverable(self, escrow_id: int) -> None:
        """
        Trigger validator consensus to judge the current submission
        against the escrow's acceptance criteria.

        Each validator independently reviews the brief, the criteria, and
        the submission, and returns a structured verdict. Validators are
        reconciled with a non-comparative equivalence principle: they must
        independently reach the same verdict category, not identical
        wording — appropriate for a judgment call rather than a
        deterministic check.
        """
        eid = u256(escrow_id)
        e = self.escrows.get(eid, None)
        if e is None:
            raise Exception("unknown escrow_id")
        if e.status != u256(STATUS_SUBMITTED):
            raise Exception("escrow has no pending submission to verify")

        brief = e.brief
        criteria = e.criteria
        submission = e.submission

        def get_verdict() -> str:
            # Non-deterministic block. Closure-captured values only, no
            # external args, must return a plain string.
            prompt = (
                "You are a neutral reviewer judging whether a submitted "
                "deliverable satisfies an agreed brief, for an on-chain "
                "escrow release decision.\n\n"
                f"Brief (what was requested): {brief}\n\n"
                f"Acceptance criteria: {criteria}\n\n"
                f"Submission (payee's proof of work): {submission}\n\n"
                "Judge the submission against the brief and criteria. "
                "Respond ONLY with JSON in this exact shape, nothing else:\n"
                '{"verdict": "APPROVED" | "REJECTED" | "NEEDS_REVISION", '
                '"reasoning": "<one or two sentences citing the specific '
                'criteria that were or were not met>"}'
            )
            return gl.nondet.exec_prompt(prompt)

        raw = gl.eq_principle.prompt_non_comparative(
            get_verdict,
            task="Judge whether a submitted deliverable satisfies an escrow's acceptance criteria.",
            criteria=(
                "Two verdicts are equivalent if they reach the same "
                "'verdict' category (APPROVED, REJECTED, or NEEDS_REVISION) "
                "given the stated brief and acceptance criteria, even if "
                "'reasoning' wording differs. If the verdict category "
                "differs, the two are NOT equivalent."
            ),
        )

        try:
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.strip("`")
                if cleaned.lower().startswith("json"):
                    cleaned = cleaned[4:]
                cleaned = cleaned.strip()
            parsed = json.loads(cleaned)
            verdict = str(parsed.get("verdict", "")).strip().upper()
            reasoning = str(parsed.get("reasoning", "")).strip()
        except Exception:
            verdict = "NEEDS_REVISION"
            reasoning = "Validator response was not valid JSON; defaulted to NEEDS_REVISION for safety."

        if verdict not in ("APPROVED", "REJECTED", "NEEDS_REVISION"):
            verdict = "NEEDS_REVISION"

        e.verdict_reasoning = reasoning[:500]
        if verdict == "APPROVED":
            e.status = u256(STATUS_APPROVED)
        else:
            # Both REJECTED and NEEDS_REVISION route back to REJECTED,
            # which re-opens submit_deliverable for a revised attempt.
            e.status = u256(STATUS_REJECTED)
        self.escrows[eid] = e

    # -----------------------------------------------------------------
    # 4a. Payee withdraws after approval
    # -----------------------------------------------------------------
    @gl.public.write
    def withdraw(self, escrow_id: int) -> None:
        """
        Payee withdraws the escrowed funds after an APPROVED verdict.
        Terminal: moves the escrow to RELEASED so it cannot be withdrawn
        twice.
        """
        eid = u256(escrow_id)
        e = self.escrows.get(eid, None)
        if e is None:
            raise Exception("unknown escrow_id")
        if gl.message.sender_address != e.payee:
            raise Exception("only the payee can withdraw")
        if e.status != u256(STATUS_APPROVED):
            raise Exception("escrow is not in an APPROVED state")

        amount = int(e.amount)
        e.status = u256(STATUS_RELEASED)
        self.escrows[eid] = e
        gl.eth_transfer(e.payee, amount)

    # -----------------------------------------------------------------
    # 4b. Payer reclaims funds after the refund window, if never approved
    # -----------------------------------------------------------------
    @gl.public.write
    def refund(self, escrow_id: int) -> None:
        """
        Payer reclaims escrowed funds if the deliverable was never
        approved and the refund_after timestamp has passed. Not available
        if refund_after was set to 0 at creation (refund path disabled),
        or once funds have already been released.
        """
        eid = u256(escrow_id)
        e = self.escrows.get(eid, None)
        if e is None:
            raise Exception("unknown escrow_id")
        if gl.message.sender_address != e.payer:
            raise Exception("only the payer can request a refund")
        if e.status in (u256(STATUS_RELEASED), u256(STATUS_REFUNDED)):
            raise Exception("escrow has already been settled")
        if e.status == u256(STATUS_APPROVED):
            raise Exception("deliverable was approved; payer cannot reclaim funds")
        if int(e.refund_after) == 0:
            raise Exception("refund path is disabled for this escrow")
        if int(gl.evm.block.timestamp) < int(e.refund_after):
            raise Exception("refund window has not opened yet")

        amount = int(e.amount)
        e.status = u256(STATUS_REFUNDED)
        self.escrows[eid] = e
        gl.eth_transfer(e.payer, amount)

    # -----------------------------------------------------------------
    # Views
    # -----------------------------------------------------------------
    @gl.public.view
    def get_status(self, escrow_id: int) -> int:
        e = self.escrows.get(u256(escrow_id), None)
        if e is None:
            raise Exception("unknown escrow_id")
        return int(e.status)

    @gl.public.view
    def get_amount(self, escrow_id: int) -> int:
        e = self.escrows.get(u256(escrow_id), None)
        if e is None:
            raise Exception("unknown escrow_id")
        return int(e.amount)

    @gl.public.view
    def get_submission(self, escrow_id: int) -> str:
        e = self.escrows.get(u256(escrow_id), None)
        if e is None:
            raise Exception("unknown escrow_id")
        return e.submission

    @gl.public.view
    def get_verdict_reasoning(self, escrow_id: int) -> str:
        e = self.escrows.get(u256(escrow_id), None)
        if e is None:
            raise Exception("unknown escrow_id")
        return e.verdict_reasoning

    @gl.public.view
    def get_payee(self, escrow_id: int) -> str:
        e = self.escrows.get(u256(escrow_id), None)
        if e is None:
            raise Exception("unknown escrow_id")
        return e.payee.as_hex

    @gl.public.view
    def escrow_count(self) -> int:
        return int(self.next_id)
