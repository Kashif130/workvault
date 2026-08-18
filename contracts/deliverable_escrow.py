# v0.3.0
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
FUNDED -> SUBMITTED -> (APPROVED -> RELEASED)
                     -> (REJECTED -> back to SUBMITTED-eligible)
                     -> (DISPUTED -> RELEASED | REFUNDED, arbiter decides)
FUNDED | SUBMITTED   -> CANCELLED (mutual consent, both parties agree)
FUNDED | SUBMITTED | REJECTED -> REFUNDED (payer, after refund delay, if
                                            refund_enabled)

1. Payer creates an escrow: deposits funds, names a payee, states the
   deliverable brief and acceptance criteria, and — if refunds are
   enabled — a delay (in seconds) the payee gets to work before the
   payer may reclaim funds.
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
   resubmit, or — if the payer enabled refunds at creation time AND the
   refund delay has elapsed since creation — the payer can reclaim funds.
5. Either party may instead raise a dispute at any point after
   submission. A dispute freezes normal refund/withdraw paths and hands
   the final call to the contract's arbiter, who resolves it toward
   either the payee (release) or the payer (refund) — an escape hatch
   for cases validator consensus can't cleanly settle (e.g. off-chain
   evidence, contested scope).
6. Either party may propose cancelling an untouched or in-flight escrow;
   if the other party agrees, funds return to the payer with no fee
   charged and no verdict needed — for jobs called off by agreement.

This separates "judge the work" (LLM consensus, can be re-run) from
"move the money" (a single, guarded withdraw path), which is the standard
escrow safety pattern applied to a subjective/judged deliverable instead
of a deterministic condition. The timed refund window exists so a payer
can't fund an escrow and reclaim it moments later before the payee had
any real chance to submit work.

Platform fee
------------
An optional small fee (basis points, e.g. 250 = 2.5%) is deducted from
the payee's release amount and sent to a configurable treasury address.
Fee is charged ONLY on a successful validator-approved withdraw — never
on refunds, cancellations, or arbiter-ordered refunds — so a payer who
never gets a deliverable never pays a fee, and the payee only pays a fee
on money they actually received for completed work. Fee defaults to 0
(disabled) and only the contract owner can change it, capped at 1000 bps
(10%) hard-coded so no owner action can siphon an unreasonable cut.

Dispute / arbiter path
-----------------------
Disputes exist for the cases neither automated consensus nor a simple
timer can resolve fairly — e.g. the payee insists validators misjudged
live content that's since changed, or the payer claims the payee's link
is broken for everyone but validators happened to catch it live. Only
payer or payee (whoever didn't just act) can raise one, only after a
submission exists, and only the contract's designated arbiter (owner by
default, reassignable) can resolve it — a single resolve_dispute call
that either releases to the payee or refunds the payer, bypassing the
normal APPROVED/refund-delay gates since a human/arbiter judgment has
already been rendered. This is a deliberately narrow escape hatch, not a
replacement for the validator path: it's only reachable after a
submission and while the escrow isn't already settled.

Changelog
---------
v0.2.0: added a real timed refund window (Unix timestamps via
        datetime.datetime.now(), matching this pinned runner's
        documented pattern) — refund() now checks elapsed time instead
        of being available immediately — and reconciled every getter
        the frontend reads (get_created_at, get_refund_available_at,
        get_submit_count added).
v0.2.1: verify_deliverable now detects a URL in the submission and has
        each validator independently fetch its live content
        (gl.nondet.web.render) to judge against, instead of trusting
        the payee's typed description alone. Falls back to judging the
        submission text as-is if there's no URL or the fetch fails.
v0.3.0: platform fee (owner-configurable, capped, charged only on
        approved withdraw, locked in per-escrow at funding time);
        dispute/arbiter escape hatch (raise_dispute/resolve_dispute);
        mutual-consent cancellation (propose_cancel, auto-resolves once
        both parties have called it); payer/payee escrow indexes with
        count+slot getters for real pagination instead of the frontend
        scanning every id; owner-controlled emergency pause on all
        state-changing calls; input hardening (zero-address checks,
        string length caps, fee/arbiter bounds); CANCELLED and DISPUTED
        statuses added to the lifecycle.
"""

from genlayer import *
from dataclasses import dataclass
import datetime
import re


# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------
STATUS_FUNDED = 0        # payer deposited, waiting on payee to submit
STATUS_SUBMITTED = 1     # payee submitted, waiting on verification
STATUS_APPROVED = 2      # validators approved, payee can withdraw
STATUS_REJECTED = 3      # validators rejected; payee may resubmit
STATUS_RELEASED = 4      # funds withdrawn by payee — terminal
STATUS_REFUNDED = 5      # funds returned to payer — terminal
STATUS_DISPUTED = 6      # under arbiter review — terminal-pending-arbiter
STATUS_CANCELLED = 7     # mutually cancelled, funds returned — terminal

ZERO_ADDRESS = Address("0x0000000000000000000000000000000000000000")

MAX_FEE_BPS = 1000          # hard cap: owner can never set fee above 10%
BPS_DENOMINATOR = 10000

MAX_TEXT_LEN = 10000         # cap on brief/criteria/submission length
                              # (storage-abuse guard; generous for real use)
MAX_DISPUTE_REASON_LEN = 2000


@allow_storage
@dataclass
class Escrow:
    payer: Address
    payee: Address
    amount: u256
    brief: str                    # what the deliverable should be
    criteria: str                  # how validators should judge it
    submission: str                 # payee's proof-of-work text/URL, empty until submitted
    status: u256
    verdict_reasoning: str
    submit_count: u256              # how many times the payee has submitted
    refund_enabled: bool            # whether the payer may reclaim funds if never approved
    created_at: u256                # real Unix timestamp, set at creation
    refund_available_at: u256       # created_at + refund_delay_seconds; refund() checks against this
    last_raw_response: str          # debug: last raw validator output before parsing
    fee_bps_at_creation: u256       # fee locked in at funding time — later fee changes never retroactively affect open escrows
    payer_cancel_vote: bool         # mutual-cancel: has the payer agreed?
    payee_cancel_vote: bool         # mutual-cancel: has the payee agreed?
    disputed_by: Address            # who raised the current/last dispute (ZERO_ADDRESS if none)
    dispute_reason: str             # free text explaining the dispute
    dispute_resolution_note: str    # arbiter's note explaining the resolution


class DeliverableEscrow(gl.Contract):
    escrows: TreeMap[u256, Escrow]
    next_id: u256
    owner: Address
    arbiter: Address
    treasury: Address
    fee_bps: u256                     # current fee in basis points, applied to NEW escrows only
    paused: bool
    # Payer/payee -> count of escrows indexed for them, plus a flat
    # (address, slot) -> escrow_id map. Avoids nested dynamic-array
    # storage types and keeps every entry a simple scalar, matching the
    # flat TreeMap[u256, X] pattern the rest of this contract (and the
    # pinned runtime it targets) already relies on.
    payer_escrow_count: TreeMap[Address, u256]
    payer_escrow_at: TreeMap[str, u256]   # key: f"{payer}:{slot}" -> escrow_id
    payee_escrow_count: TreeMap[Address, u256]
    payee_escrow_at: TreeMap[str, u256]   # key: f"{payee}:{slot}" -> escrow_id

    def __init__(self):
        self.next_id = u256(0)
        self.owner = gl.message.sender_address
        self.arbiter = gl.message.sender_address
        self.treasury = gl.message.sender_address
        self.fee_bps = u256(0)
        self.paused = False

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------
    def _require_not_paused(self) -> None:
        if self.paused:
            raise Exception("contract is paused")

    def _require_owner(self) -> None:
        if gl.message.sender_address != self.owner:
            raise Exception("only the contract owner may call this")

    def _get_escrow_or_raise(self, escrow_id: int) -> Escrow:
        e = self.escrows.get(u256(escrow_id), None)
        if e is None:
            raise Exception("unknown escrow_id")
        return e

    def _index_key(self, addr: Address, slot: int) -> str:
        # Normalize to lowercase so lookups never depend on the caller
        # passing back an address in the exact same case they gave
        # originally (checksum vs. lowercase hex, etc.).
        return str(addr).lower() + ":" + str(slot)

    def _append_payer_index(self, addr: Address, eid: u256) -> None:
        count = self.payer_escrow_count.get(addr, u256(0))
        key = self._index_key(addr, int(count))
        self.payer_escrow_at[key] = eid
        self.payer_escrow_count[addr] = u256(int(count) + 1)

    def _append_payee_index(self, addr: Address, eid: u256) -> None:
        count = self.payee_escrow_count.get(addr, u256(0))
        key = self._index_key(addr, int(count))
        self.payee_escrow_at[key] = eid
        self.payee_escrow_count[addr] = u256(int(count) + 1)

    # -----------------------------------------------------------------
    # Owner / admin controls
    # -----------------------------------------------------------------
    @gl.public.write
    def set_fee_bps(self, new_fee_bps: int) -> None:
        """Owner-only. Sets the platform fee (basis points) applied to
        escrows created FROM THIS POINT ON. Existing open escrows keep
        the fee rate that was locked in when they were funded, so a fee
        change never retroactively affects money already in flight.
        Hard-capped at MAX_FEE_BPS regardless of what the owner passes."""
        self._require_owner()
        if new_fee_bps < 0:
            raise Exception("fee cannot be negative")
        if new_fee_bps > MAX_FEE_BPS:
            raise Exception(f"fee cannot exceed {MAX_FEE_BPS} bps ({MAX_FEE_BPS / 100}%)")
        self.fee_bps = u256(new_fee_bps)

    @gl.public.write
    def set_treasury(self, new_treasury: str) -> None:
        """Owner-only. Where platform fees are sent."""
        self._require_owner()
        addr = Address(new_treasury)
        if addr == ZERO_ADDRESS:
            raise Exception("treasury cannot be the zero address")
        self.treasury = addr

    @gl.public.write
    def set_arbiter(self, new_arbiter: str) -> None:
        """Owner-only. Reassigns who can resolve disputes. Defaults to
        the contract owner at deploy time; separating the roles lets a
        team designate a neutral arbiter distinct from the deployer."""
        self._require_owner()
        addr = Address(new_arbiter)
        if addr == ZERO_ADDRESS:
            raise Exception("arbiter cannot be the zero address")
        self.arbiter = addr

    @gl.public.write
    def transfer_ownership(self, new_owner: str) -> None:
        """Owner-only. Transfers admin control of the contract."""
        self._require_owner()
        addr = Address(new_owner)
        if addr == ZERO_ADDRESS:
            raise Exception("new owner cannot be the zero address")
        self.owner = addr

    @gl.public.write
    def set_paused(self, paused: bool) -> None:
        """Owner-only emergency stop. While paused, no new escrows can
        be created and no state-changing action can be taken on
        existing escrows (views remain readable). Does not touch funds
        directly — it only blocks further actions until unpaused."""
        self._require_owner()
        self.paused = paused

    # -----------------------------------------------------------------
    # 1. Fund an escrow
    # -----------------------------------------------------------------
    @gl.public.write.payable
    def create_escrow(
        self,
        payee: str,
        brief: str,
        criteria: str,
        refund_enabled: bool,
        refund_delay_seconds: int,
    ) -> None:
        """
        Deposit funds into a new escrow.

        payee:                  address (as a hex string) that will submit
                                 the deliverable and receive funds on
                                 approval.
        brief:                  what is being paid for, e.g. "A 500-word
                                 blog post about GenLayer's consensus
                                 model."
        criteria:                how validators should judge the
                                 submission, e.g. "Approve if the linked
                                 post is at least 400 words, specifically
                                 discusses GenLayer, and is not
                                 plagiarized boilerplate."
        refund_enabled:          if True, the payer may call refund()
                                 once refund_delay_seconds have elapsed
                                 since creation, as long as the escrow is
                                 not APPROVED/RELEASED/REFUNDED/DISPUTED/
                                 CANCELLED. If False, funds can only ever
                                 leave via an APPROVED verdict +
                                 withdraw(), a mutual cancel, or an
                                 arbiter-ordered refund.
        refund_delay_seconds:    how long (in seconds) the payee gets to
                                 work before the payer may reclaim funds.
                                 Ignored if refund_enabled is False. Use
                                 0 for "refundable immediately if never
                                 approved."
        """
        self._require_not_paused()
        if gl.message.value <= 0:
            raise Exception("escrow must be funded with a positive amount")
        if len(brief.strip()) == 0:
            raise Exception("brief cannot be empty")
        if len(brief) > MAX_TEXT_LEN:
            raise Exception(f"brief exceeds max length of {MAX_TEXT_LEN} characters")
        if len(criteria.strip()) == 0:
            raise Exception("acceptance criteria cannot be empty")
        if len(criteria) > MAX_TEXT_LEN:
            raise Exception(f"criteria exceeds max length of {MAX_TEXT_LEN} characters")
        if refund_delay_seconds < 0:
            raise Exception("refund_delay_seconds cannot be negative")

        payee_addr = Address(payee)
        if payee_addr == ZERO_ADDRESS:
            raise Exception("payee cannot be the zero address")
        if payee_addr == gl.message.sender_address:
            raise Exception("payee cannot be the same as payer")

        eid = self.next_id
        self.next_id = u256(int(self.next_id) + 1)

        now_ts = int(datetime.datetime.now().timestamp())
        payer_addr = gl.message.sender_address

        e = Escrow(
            payer=payer_addr,
            payee=payee_addr,
            amount=u256(int(gl.message.value)),
            brief=brief,
            criteria=criteria,
            submission="",
            status=u256(STATUS_FUNDED),
            verdict_reasoning="",
            submit_count=u256(0),
            refund_enabled=refund_enabled,
            created_at=u256(now_ts),
            refund_available_at=u256(now_ts + int(refund_delay_seconds)),
            last_raw_response="",
            fee_bps_at_creation=self.fee_bps,
            payer_cancel_vote=False,
            payee_cancel_vote=False,
            disputed_by=ZERO_ADDRESS,
            dispute_reason="",
            dispute_resolution_note="",
        )
        self.escrows[eid] = e
        self._append_payer_index(payer_addr, eid)
        self._append_payee_index(payee_addr, eid)


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
        self._require_not_paused()
        eid = u256(escrow_id)
        e = self._get_escrow_or_raise(escrow_id)
        if gl.message.sender_address != e.payee:
            raise Exception("only the designated payee can submit")
        if e.status not in (u256(STATUS_FUNDED), u256(STATUS_REJECTED)):
            raise Exception("escrow is not in a state that accepts submissions")
        if len(submission.strip()) == 0:
            raise Exception("submission cannot be empty")
        if len(submission) > MAX_TEXT_LEN:
            raise Exception(f"submission exceeds max length of {MAX_TEXT_LEN} characters")

        e.submission = submission
        e.status = u256(STATUS_SUBMITTED)
        e.submit_count = u256(int(e.submit_count) + 1)
        # A fresh submission resets any stale cancel votes from a prior
        # round so an old "I agree to cancel" doesn't silently apply to
        # a brand-new piece of work.
        e.payer_cancel_vote = False
        e.payee_cancel_vote = False
        self.escrows[eid] = e


    # -----------------------------------------------------------------
    # 3. Verify — the non-deterministic consensus step
    # -----------------------------------------------------------------
    @gl.public.write
    def verify_deliverable(self, escrow_id: int) -> None:
        """
        Trigger validator consensus to judge the current submission
        against the escrow's acceptance criteria.

        If the submission contains a URL, each validator independently
        fetches that URL's live content (via gl.nondet.web.render) and
        judges against what's actually there — not just the payee's
        self-reported description. This is what lets WorkVault settle
        claims like "is the linked blog post at least 300 words and
        does it cover X" against authoritative live data instead of
        trusting the submission text alone. If the submission has no
        URL, or the fetch fails, validators fall back to judging the
        submission text as-is (with the fetch failure surfaced in the
        reasoning, not silently swallowed).

        Each validator independently reviews the brief, the criteria,
        the submission, and (if present) the live-fetched link content,
        and returns a structured verdict. Validators are reconciled
        with a non-comparative equivalence principle: they must
        independently reach the same verdict category, not identical
        wording — appropriate for a judgment call rather than a
        deterministic check.
        """
        self._require_not_paused()
        eid = u256(escrow_id)
        e = self._get_escrow_or_raise(escrow_id)
        if e.status != u256(STATUS_SUBMITTED):
            raise Exception("escrow has no pending submission to verify")

        brief = e.brief
        criteria = e.criteria
        submission = e.submission

        def get_verdict() -> str:
            url_match = re.search(r"https?://\S+", submission)
            fetched_content = ""
            if url_match:
                url = url_match.group(0).rstrip(".,;)\"'")
                try:
                    fetched_content = gl.nondet.web.render(url, mode="text")[:6000]
                except Exception as fetch_err:
                    fetched_content = (
                        f"[Could not fetch the linked URL ({url}): {fetch_err}. "
                        "Judge based on the submission text alone and note "
                        "the fetch failure in your reasoning.]"
                    )

            prompt = (
                "You are judging whether a submitted deliverable satisfies "
                "an escrow's stated acceptance criteria.\n\n"
                f"BRIEF (what was supposed to be delivered):\n{brief}\n\n"
                f"ACCEPTANCE CRITERIA (how to judge it):\n{criteria}\n\n"
                f"SUBMISSION (payee's description/link):\n{submission}\n\n"
            )
            if fetched_content:
                prompt += (
                    "LIVE CONTENT FETCHED FROM THE SUBMITTED LINK (this is "
                    "the actual current content at that URL right now — "
                    "judge the real deliverable against this, not just the "
                    "payee's description above):\n"
                    f"{fetched_content}\n\n"
                )
            prompt += (
                "Respond in exactly two lines, nothing else:\n"
                "Line 1: one word — APPROVED, REJECTED, or NEEDS_REVISION\n"
                "Line 2: one short sentence explaining why, citing the "
                "specific criteria that were or were not met"
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

        e.last_raw_response = raw[:800]

        # Keyword-based extraction instead of strict JSON parsing: this
        # pinned GenVM/model combination does not reliably return
        # structured JSON even when explicitly instructed to, so we parse
        # defensively by scanning for the verdict keyword instead of
        # depending on an exact machine-readable shape.
        upper = raw.upper()
        if "NEEDS_REVISION" in upper or "NEEDS REVISION" in upper:
            verdict = "NEEDS_REVISION"
        elif "APPROVED" in upper:
            verdict = "APPROVED"
        elif "REJECTED" in upper:
            verdict = "REJECTED"
        else:
            verdict = "NEEDS_REVISION"

        # Reasoning: everything after the first line (the verdict word),
        # falling back to the full raw response if there's no second line.
        lines = raw.strip().splitlines()
        if len(lines) > 1:
            reasoning = " ".join(line.strip() for line in lines[1:] if line.strip())
        else:
            reasoning = raw.strip()
        if not reasoning:
            reasoning = "No reasoning text returned by validator."

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
    # 4a. Payee withdraws after approval (fee deducted here)
    # -----------------------------------------------------------------
    @gl.public.write
    def withdraw(self, escrow_id: int) -> None:
        """
        Payee withdraws the escrowed funds after an APPROVED verdict.
        If a platform fee was in effect when this escrow was created,
        that fee (fixed at funding time, never a later rate) is
        deducted here and sent to the treasury; the remainder goes to
        the payee. Terminal: moves the escrow to RELEASED so it cannot
        be withdrawn twice.
        """
        self._require_not_paused()
        eid = u256(escrow_id)
        e = self._get_escrow_or_raise(escrow_id)
        if gl.message.sender_address != e.payee:
            raise Exception("only the payee can withdraw")
        if e.status != u256(STATUS_APPROVED):
            raise Exception("escrow is not in an APPROVED state")

        amount = int(e.amount)
        fee_bps = int(e.fee_bps_at_creation)
        fee_amount = (amount * fee_bps) // BPS_DENOMINATOR
        payee_amount = amount - fee_amount

        payee_addr = e.payee
        treasury_addr = self.treasury
        e.status = u256(STATUS_RELEASED)
        self.escrows[eid] = e

        gl.get_contract_at(payee_addr).emit_transfer(value=u256(payee_amount))
        if fee_amount > 0:
            gl.get_contract_at(treasury_addr).emit_transfer(value=u256(fee_amount))


    # -----------------------------------------------------------------
    # 4b. Payer reclaims funds after the refund window, if never approved
    # -----------------------------------------------------------------
    @gl.public.write
    def refund(self, escrow_id: int) -> None:
        """
        Payer reclaims escrowed funds if the deliverable was never
        approved. Only available if refund_enabled was set to True at
        creation, the current Unix time is at or past
        refund_available_at, and the escrow is not APPROVED or already
        settled (RELEASED/REFUNDED/DISPUTED/CANCELLED). No fee is ever
        charged on a refund.
        """
        self._require_not_paused()
        eid = u256(escrow_id)
        e = self._get_escrow_or_raise(escrow_id)
        if gl.message.sender_address != e.payer:
            raise Exception("only the payer can request a refund")
        if e.status in (
            u256(STATUS_RELEASED),
            u256(STATUS_REFUNDED),
            u256(STATUS_DISPUTED),
            u256(STATUS_CANCELLED),
        ):
            raise Exception("escrow has already been settled or is under dispute")
        if e.status == u256(STATUS_APPROVED):
            raise Exception("deliverable was approved; payer cannot reclaim funds")
        if not e.refund_enabled:
            raise Exception("refund path is disabled for this escrow")

        now_ts = int(datetime.datetime.now().timestamp())
        if now_ts < int(e.refund_available_at):
            raise Exception(
                "refund not available yet — unlocks at unix timestamp "
                + str(int(e.refund_available_at))
            )

        amount = int(e.amount)
        payer_addr = e.payer
        e.status = u256(STATUS_REFUNDED)
        self.escrows[eid] = e
        gl.get_contract_at(payer_addr).emit_transfer(value=u256(amount))


    # -----------------------------------------------------------------
    # 5. Mutual-consent cancellation (no verdict needed either way)
    # -----------------------------------------------------------------
    @gl.public.write
    def propose_cancel(self, escrow_id: int) -> None:
        """
        Either the payer or payee registers their consent to cancel an
        escrow that hasn't been settled yet. Once BOTH parties have
        called this (in either order), the escrow is auto-cancelled and
        the full amount — no fee — returns to the payer. Not available
        once APPROVED (the payee has already earned it), or once the
        escrow is already RELEASED/REFUNDED/DISPUTED/CANCELLED.
        """
        self._require_not_paused()
        eid = u256(escrow_id)
        e = self._get_escrow_or_raise(escrow_id)
        sender = gl.message.sender_address
        if sender not in (e.payer, e.payee):
            raise Exception("only the payer or payee can propose cancellation")
        if e.status in (
            u256(STATUS_APPROVED),
            u256(STATUS_RELEASED),
            u256(STATUS_REFUNDED),
            u256(STATUS_DISPUTED),
            u256(STATUS_CANCELLED),
        ):
            raise Exception("escrow cannot be cancelled from its current state")

        if sender == e.payer:
            e.payer_cancel_vote = True
        if sender == e.payee:
            e.payee_cancel_vote = True

        if e.payer_cancel_vote and e.payee_cancel_vote:
            amount = int(e.amount)
            payer_addr = e.payer
            e.status = u256(STATUS_CANCELLED)
            self.escrows[eid] = e
            gl.get_contract_at(payer_addr).emit_transfer(value=u256(amount))
        else:
            self.escrows[eid] = e

    # -----------------------------------------------------------------
    # 6. Dispute / arbiter escape hatch
    # -----------------------------------------------------------------
    @gl.public.write
    def raise_dispute(self, escrow_id: int, reason: str) -> None:
        """
        Either party escalates an escrow to the arbiter for manual
        resolution. Only callable once a submission exists (there's
        nothing to dispute before that — use propose_cancel instead),
        and only while the escrow isn't already settled. Freezes the
        normal refund/withdraw paths; only resolve_dispute (arbiter-only)
        can move the escrow forward from here.
        """
        self._require_not_paused()
        eid = u256(escrow_id)
        e = self._get_escrow_or_raise(escrow_id)
        sender = gl.message.sender_address
        if sender not in (e.payer, e.payee):
            raise Exception("only the payer or payee can raise a dispute")
        if e.status in (
            u256(STATUS_FUNDED),
            u256(STATUS_RELEASED),
            u256(STATUS_REFUNDED),
            u256(STATUS_DISPUTED),
            u256(STATUS_CANCELLED),
        ):
            raise Exception("escrow is not in a disputable state (needs a submission and must not be settled)")
        if len(reason.strip()) == 0:
            raise Exception("dispute reason cannot be empty")
        if len(reason) > MAX_DISPUTE_REASON_LEN:
            raise Exception(f"dispute reason exceeds max length of {MAX_DISPUTE_REASON_LEN} characters")

        e.status = u256(STATUS_DISPUTED)
        e.disputed_by = sender
        e.dispute_reason = reason
        e.dispute_resolution_note = ""
        self.escrows[eid] = e


    @gl.public.write
    def resolve_dispute(self, escrow_id: int, release_to_payee: bool, resolution_note: str) -> None:
        """
        Arbiter-only. Settles a DISPUTED escrow either way:
        release_to_payee=True sends the full amount to the payee (no
        fee — disputes are a manual judgment override, not a normal
        approved release) and moves status to RELEASED;
        release_to_payee=False refunds the full amount to the payer and
        moves status to REFUNDED. Either way this is terminal.
        """
        self._require_not_paused()
        eid = u256(escrow_id)
        e = self._get_escrow_or_raise(escrow_id)
        if gl.message.sender_address != self.arbiter:
            raise Exception("only the arbiter can resolve a dispute")
        if e.status != u256(STATUS_DISPUTED):
            raise Exception("escrow is not currently under dispute")
        if len(resolution_note.strip()) == 0:
            raise Exception("resolution note cannot be empty")
        if len(resolution_note) > MAX_DISPUTE_REASON_LEN:
            raise Exception(f"resolution note exceeds max length of {MAX_DISPUTE_REASON_LEN} characters")

        amount = int(e.amount)
        e.dispute_resolution_note = resolution_note

        if release_to_payee:
            payee_addr = e.payee
            e.status = u256(STATUS_RELEASED)
            self.escrows[eid] = e
            gl.get_contract_at(payee_addr).emit_transfer(value=u256(amount))
        else:
            payer_addr = e.payer
            e.status = u256(STATUS_REFUNDED)
            self.escrows[eid] = e
            gl.get_contract_at(payer_addr).emit_transfer(value=u256(amount))

    # -----------------------------------------------------------------
    # Views
    # -----------------------------------------------------------------
    @gl.public.view
    def get_status(self, escrow_id: int) -> int:
        return int(self._get_escrow_or_raise(escrow_id).status)

    @gl.public.view
    def get_amount(self, escrow_id: int) -> int:
        return int(self._get_escrow_or_raise(escrow_id).amount)

    @gl.public.view
    def get_submission(self, escrow_id: int) -> str:
        return self._get_escrow_or_raise(escrow_id).submission

    @gl.public.view
    def get_verdict_reasoning(self, escrow_id: int) -> str:
        return self._get_escrow_or_raise(escrow_id).verdict_reasoning

    @gl.public.view
    def get_brief(self, escrow_id: int) -> str:
        return self._get_escrow_or_raise(escrow_id).brief

    @gl.public.view
    def get_criteria(self, escrow_id: int) -> str:
        return self._get_escrow_or_raise(escrow_id).criteria

    @gl.public.view
    def get_payer(self, escrow_id: int) -> str:
        return str(self._get_escrow_or_raise(escrow_id).payer)

    @gl.public.view
    def get_payee(self, escrow_id: int) -> str:
        return str(self._get_escrow_or_raise(escrow_id).payee)

    @gl.public.view
    def get_refund_enabled(self, escrow_id: int) -> bool:
        return self._get_escrow_or_raise(escrow_id).refund_enabled

    @gl.public.view
    def get_created_at(self, escrow_id: int) -> int:
        """Unix timestamp (seconds) the escrow was created at."""
        return int(self._get_escrow_or_raise(escrow_id).created_at)

    @gl.public.view
    def get_refund_available_at(self, escrow_id: int) -> int:
        """Unix timestamp (seconds) at/after which refund() becomes
        callable, if refund_enabled is True. Meaningless if
        refund_enabled is False."""
        return int(self._get_escrow_or_raise(escrow_id).refund_available_at)

    @gl.public.view
    def get_submit_count(self, escrow_id: int) -> int:
        return int(self._get_escrow_or_raise(escrow_id).submit_count)

    @gl.public.view
    def get_last_raw_response(self, escrow_id: int) -> str:
        """Debug helper: the raw (unparsed) validator output from the last
        verify_deliverable call, so a mis-parse can be diagnosed."""
        return self._get_escrow_or_raise(escrow_id).last_raw_response

    @gl.public.view
    def get_fee_bps_at_creation(self, escrow_id: int) -> int:
        """The platform fee rate (basis points) locked in for this
        specific escrow at funding time — unaffected by later
        set_fee_bps calls."""
        return int(self._get_escrow_or_raise(escrow_id).fee_bps_at_creation)

    @gl.public.view
    def get_payer_cancel_vote(self, escrow_id: int) -> bool:
        return self._get_escrow_or_raise(escrow_id).payer_cancel_vote

    @gl.public.view
    def get_payee_cancel_vote(self, escrow_id: int) -> bool:
        return self._get_escrow_or_raise(escrow_id).payee_cancel_vote

    @gl.public.view
    def get_disputed_by(self, escrow_id: int) -> str:
        """Address that raised the current/last dispute, or '' if no
        dispute has ever been raised on this escrow."""
        e = self._get_escrow_or_raise(escrow_id)
        return "" if e.disputed_by == ZERO_ADDRESS else str(e.disputed_by)

    @gl.public.view
    def get_dispute_reason(self, escrow_id: int) -> str:
        return self._get_escrow_or_raise(escrow_id).dispute_reason

    @gl.public.view
    def get_dispute_resolution_note(self, escrow_id: int) -> str:
        return self._get_escrow_or_raise(escrow_id).dispute_resolution_note

    @gl.public.view
    def get_fee_bps(self) -> int:
        """Current platform fee (basis points) that will apply to any
        NEW escrow created from now on."""
        return int(self.fee_bps)

    @gl.public.view
    def get_treasury(self) -> str:
        return str(self.treasury)

    @gl.public.view
    def get_owner(self) -> str:
        return str(self.owner)

    @gl.public.view
    def get_arbiter(self) -> str:
        return str(self.arbiter)

    @gl.public.view
    def get_paused(self) -> bool:
        return self.paused

    @gl.public.view
    def escrow_count(self) -> int:
        return int(self.next_id)

    @gl.public.view
    def get_payer_escrow_count(self, payer: str) -> int:
        """How many escrows the given address has created as payer.
        Use with get_escrow_id_for_payer_at(payer, slot) for 0..count-1
        to enumerate them — the same pagination pattern as
        escrow_count()/escrow ids, applied per-address."""
        return int(self.payer_escrow_count.get(Address(payer), u256(0)))

    @gl.public.view
    def get_escrow_id_for_payer_at(self, payer: str, slot: int) -> int:
        """The escrow_id at position `slot` (0-indexed, creation order)
        among escrows where the given address is the payer."""
        key = self._index_key(Address(payer), slot)
        val = self.payer_escrow_at.get(key, None)
        if val is None:
            raise Exception("no escrow at that slot for this payer")
        return int(val)

    @gl.public.view
    def get_payee_escrow_count(self, payee: str) -> int:
        """How many escrows the given address has been named payee on."""
        return int(self.payee_escrow_count.get(Address(payee), u256(0)))

    @gl.public.view
    def get_escrow_id_for_payee_at(self, payee: str, slot: int) -> int:
        """The escrow_id at position `slot` (0-indexed, creation order)
        among escrows where the given address is the payee."""
        key = self._index_key(Address(payee), slot)
        val = self.payee_escrow_at.get(key, None)
        if val is None:
            raise Exception("no escrow at that slot for this payee")
        return int(val)
