"""
test_deliverable_escrow.py — repository tests for DeliverableEscrow v0.3.0.

Built on the `genlayer-test` framework (pytest + genlayer-py), which
deploys the contract against a real GenLayer network (Studio/localnet)
and drives it exactly the way the frontend does: reads via `.call()`,
writes via `.transact()`.

Run with:
    pip install genlayer-test
    gltest test_deliverable_escrow.py
    # or, if configured as a pytest plugin: pytest test_deliverable_escrow.py

Requires a running GenLayer Studio/localnet instance the test runner is
pointed at (see genlayer-test docs for network configuration) — these
are integration tests, not offline unit tests, since the contract's
core behavior (verify_deliverable) depends on live validator consensus.

Coverage:
  - create_escrow, all getters              -> test_create_and_read,
                                                test_getter_reconciliation
  - submit_deliverable                      -> test_submit_and_verify
  - verify_deliverable                      -> test_submit_and_verify
  - withdraw (+ fee deduction)              -> test_approve_and_withdraw_end_to_end,
                                                test_fee_deducted_on_withdraw
  - timed refund                            -> test_refund_before_and_after_delay,
                                                test_refund_disabled_never_available
  - mutual cancel                           -> test_mutual_cancel
  - disputes / arbiter                      -> test_dispute_resolve_to_payee,
                                                test_dispute_resolve_to_payer,
                                                test_dispute_wrong_caller_rejected
  - admin controls (owner/arbiter/treasury/pause) -> test_admin_controls,
                                                test_pause_blocks_state_changes,
                                                test_transfer_ownership
  - payer/payee indexes                     -> test_payer_payee_index
  - input hardening                         -> test_create_rejects_empty_funding,
                                                test_create_rejects_self_payee,
                                                test_create_rejects_zero_address_payee
"""

import time

import pytest
from gltest import get_contract_factory, get_default_account, create_account
from gltest.assertions import tx_execution_succeeded, tx_execution_failed


CONTRACT_NAME = "DeliverableEscrow"

BRIEF = "Write a 100-word summary of GenLayer's consensus model."
CRITERIA = "Approve if the submission is at least 80 words and mentions validators."
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


@pytest.fixture
def payer():
    return get_default_account()


@pytest.fixture
def payee():
    return create_account()


@pytest.fixture
def contract(payer):
    factory = get_contract_factory(CONTRACT_NAME)
    return factory.deploy(account=payer)


# ---------------------------------------------------------------------
# 1. create_escrow + every read the frontend depends on
# ---------------------------------------------------------------------
def test_create_and_read(contract, payer, payee):
    tx = contract.create_escrow(
        args=[str(payee.address), BRIEF, CRITERIA, True, 0],
        value=1000,
    ).transact(account=payer)
    assert tx_execution_succeeded(tx)

    assert contract.escrow_count().call() == 1
    assert contract.get_status(args=[0]).call() == 0  # FUNDED
    assert contract.get_amount(args=[0]).call() == 1000
    assert contract.get_brief(args=[0]).call() == BRIEF
    assert contract.get_criteria(args=[0]).call() == CRITERIA
    assert contract.get_payer(args=[0]).call().lower() == str(payer.address).lower()
    assert contract.get_payee(args=[0]).call().lower() == str(payee.address).lower()
    assert contract.get_refund_enabled(args=[0]).call() is True
    assert contract.get_submission(args=[0]).call() == ""
    assert contract.get_submit_count(args=[0]).call() == 0
    assert contract.get_fee_bps_at_creation(args=[0]).call() == 0
    assert contract.get_payer_cancel_vote(args=[0]).call() is False
    assert contract.get_payee_cancel_vote(args=[0]).call() is False
    assert contract.get_disputed_by(args=[0]).call() == ""

    created_at = contract.get_created_at(args=[0]).call()
    refund_at = contract.get_refund_available_at(args=[0]).call()
    assert refund_at == created_at


def test_create_rejects_empty_funding(contract, payer, payee):
    tx = contract.create_escrow(
        args=[str(payee.address), BRIEF, CRITERIA, True, 0],
        value=0,
    ).transact(account=payer)
    assert tx_execution_failed(tx)


def test_create_rejects_self_payee(contract, payer):
    tx = contract.create_escrow(
        args=[str(payer.address), BRIEF, CRITERIA, True, 0],
        value=1000,
    ).transact(account=payer)
    assert tx_execution_failed(tx)


def test_create_rejects_zero_address_payee(contract, payer):
    tx = contract.create_escrow(
        args=[ZERO_ADDRESS, BRIEF, CRITERIA, True, 0],
        value=1000,
    ).transact(account=payer)
    assert tx_execution_failed(tx)


def test_create_rejects_oversized_text(contract, payer, payee):
    huge_brief = "x" * 10001
    tx = contract.create_escrow(
        args=[str(payee.address), huge_brief, CRITERIA, True, 0],
        value=1000,
    ).transact(account=payer)
    assert tx_execution_failed(tx)


# ---------------------------------------------------------------------
# 2 & 3. submit_deliverable + verify_deliverable
# ---------------------------------------------------------------------
def test_submit_and_verify(contract, payer, payee):
    contract.create_escrow(
        args=[str(payee.address), BRIEF, CRITERIA, True, 0],
        value=1000,
    ).transact(account=payer)

    submission = (
        "GenLayer is a blockchain where validator nodes powered by "
        "diverse AI models reach consensus on subjective decisions. "
        "Validators independently review submissions and reconcile "
        "their judgments through an equivalence principle, allowing "
        "the chain to settle claims that plain deterministic code "
        "cannot evaluate on its own, such as natural-language judgment "
        "calls about whether a deliverable meets stated criteria."
    )
    tx = contract.submit_deliverable(
        args=[0, submission]
    ).transact(account=payee)
    assert tx_execution_succeeded(tx)
    assert contract.get_status(args=[0]).call() == 1  # SUBMITTED
    assert contract.get_submission(args=[0]).call() == submission
    assert contract.get_submit_count(args=[0]).call() == 1

    # only the designated payee may submit
    other = create_account()
    bad_tx = contract.submit_deliverable(
        args=[0, "not the payee"]
    ).transact(account=other)
    assert tx_execution_failed(bad_tx)

    # trigger validator consensus — this calls into live LLM validators,
    # so it's slow and its exact verdict isn't asserted here, only that
    # the call completes and the status moves out of SUBMITTED
    verify_tx = contract.verify_deliverable(args=[0]).transact(account=payer)
    assert tx_execution_succeeded(verify_tx)
    status_after = contract.get_status(args=[0]).call()
    assert status_after in (2, 3)  # APPROVED or REJECTED
    assert contract.get_verdict_reasoning(args=[0]).call() != ""


# ---------------------------------------------------------------------
# 4. withdraw
# ---------------------------------------------------------------------
def test_withdraw_requires_approved_status(contract, payer, payee):
    contract.create_escrow(
        args=[str(payee.address), BRIEF, CRITERIA, True, 0],
        value=1000,
    ).transact(account=payer)

    tx = contract.withdraw(args=[0]).transact(account=payee)
    assert tx_execution_failed(tx)

    other = create_account()
    tx2 = contract.withdraw(args=[0]).transact(account=other)
    assert tx_execution_failed(tx2)


GOOD_SUBMISSION = (
    "GenLayer is a blockchain where independent validator nodes, "
    "each backed by a different AI model, reach consensus on "
    "subjective questions rather than only deterministic ones. "
    "Instead of requiring identical output across validators, "
    "GenLayer uses equivalence principles so validators can agree "
    "that two different-but-compatible answers both satisfy a "
    "task's criteria. This lets Intelligent Contracts fetch live "
    "web data, run natural-language judgment, and settle claims "
    "that traditional smart contracts would need an external "
    "oracle for, all while remaining verifiable on-chain through "
    "the same validator consensus that secures ordinary state "
    "transitions."
)


def test_approve_and_withdraw_end_to_end(contract, payer, payee):
    """
    Full happy path through live validator consensus. Slower and
    depends on the pinned model returning an APPROVED verdict for an
    on-brief, on-criteria submission — kept separate from the
    deterministic tests above so a flaky LLM round doesn't mask a real
    regression in the other cases.
    """
    contract.create_escrow(
        args=[str(payee.address), BRIEF, CRITERIA, True, 0],
        value=1000,
    ).transact(account=payer)

    contract.submit_deliverable(args=[0, GOOD_SUBMISSION]).transact(account=payee)
    contract.verify_deliverable(args=[0]).transact(account=payer)

    status = contract.get_status(args=[0]).call()
    if status != 2:
        pytest.skip("validator consensus did not return APPROVED for this run; "
                    "see get_verdict_reasoning for why")

    payee_balance_before = payee.balance
    tx = contract.withdraw(args=[0]).transact(account=payee)
    assert tx_execution_succeeded(tx)
    assert contract.get_status(args=[0]).call() == 4  # RELEASED
    assert payee.balance > payee_balance_before

    tx2 = contract.withdraw(args=[0]).transact(account=payee)
    assert tx_execution_failed(tx2)


def test_fee_deducted_on_withdraw(contract, payer, payee):
    """
    Sets a 5% fee before funding, confirms it's locked into the escrow
    at creation, then (if validator consensus approves) confirms the
    payee receives amount-minus-fee and the fee lands with treasury.
    """
    owner = payer  # default account is owner at deploy time
    set_tx = contract.set_fee_bps(args=[500]).transact(account=owner)  # 5%
    assert tx_execution_succeeded(set_tx)
    assert contract.get_fee_bps().call() == 500

    contract.create_escrow(
        args=[str(payee.address), BRIEF, CRITERIA, True, 0],
        value=10000,
    ).transact(account=payer)
    assert contract.get_fee_bps_at_creation(args=[0]).call() == 500

    # changing the fee after creation must not affect this escrow
    contract.set_fee_bps(args=[0]).transact(account=owner)
    assert contract.get_fee_bps_at_creation(args=[0]).call() == 500

    contract.submit_deliverable(args=[0, GOOD_SUBMISSION]).transact(account=payee)
    contract.verify_deliverable(args=[0]).transact(account=payer)
    if contract.get_status(args=[0]).call() != 2:
        pytest.skip("validator consensus did not return APPROVED for this run")

    treasury_before = owner.balance  # treasury defaults to owner/deployer
    payee_before = payee.balance
    tx = contract.withdraw(args=[0]).transact(account=payee)
    assert tx_execution_succeeded(tx)
    # payee got 95% (10000 - 500 bps fee = 9500), treasury got the 5% (500)
    assert payee.balance - payee_before == 9500
    assert owner.balance - treasury_before == 500


# ---------------------------------------------------------------------
# 5. Timed refund
# ---------------------------------------------------------------------
def test_refund_before_and_after_delay(contract, payer, payee):
    delay_seconds = 3
    contract.create_escrow(
        args=[str(payee.address), BRIEF, CRITERIA, True, delay_seconds],
        value=1000,
    ).transact(account=payer)

    created_at = contract.get_created_at(args=[0]).call()
    refund_at = contract.get_refund_available_at(args=[0]).call()
    assert refund_at == created_at + delay_seconds

    early_tx = contract.refund(args=[0]).transact(account=payer)
    assert tx_execution_failed(early_tx)
    assert contract.get_status(args=[0]).call() == 0  # still FUNDED

    other = create_account()
    time.sleep(delay_seconds + 1)
    wrong_caller_tx = contract.refund(args=[0]).transact(account=other)
    assert tx_execution_failed(wrong_caller_tx)

    payer_balance_before = payer.balance
    tx = contract.refund(args=[0]).transact(account=payer)
    assert tx_execution_succeeded(tx)
    assert contract.get_status(args=[0]).call() == 5  # REFUNDED
    assert payer.balance > payer_balance_before

    tx2 = contract.refund(args=[0]).transact(account=payer)
    assert tx_execution_failed(tx2)


def test_refund_disabled_never_available(contract, payer, payee):
    contract.create_escrow(
        args=[str(payee.address), BRIEF, CRITERIA, False, 0],
        value=1000,
    ).transact(account=payer)

    tx = contract.refund(args=[0]).transact(account=payer)
    assert tx_execution_failed(tx)


# ---------------------------------------------------------------------
# 6. Mutual-consent cancellation
# ---------------------------------------------------------------------
def test_mutual_cancel(contract, payer, payee):
    contract.create_escrow(
        args=[str(payee.address), BRIEF, CRITERIA, False, 0],  # refund disabled on purpose
        value=1000,
    ).transact(account=payer)

    # payer proposes first — should not auto-cancel yet
    tx1 = contract.propose_cancel(args=[0]).transact(account=payer)
    assert tx_execution_succeeded(tx1)
    assert contract.get_status(args=[0]).call() == 0  # still FUNDED
    assert contract.get_payer_cancel_vote(args=[0]).call() is True
    assert contract.get_payee_cancel_vote(args=[0]).call() is False

    # payee agrees — should now auto-cancel and refund the payer, even
    # though refund_enabled was False (cancel is a separate path)
    payer_balance_before = payer.balance
    tx2 = contract.propose_cancel(args=[0]).transact(account=payee)
    assert tx_execution_succeeded(tx2)
    assert contract.get_status(args=[0]).call() == 7  # CANCELLED
    assert payer.balance > payer_balance_before


def test_cancel_blocked_after_approval(contract, payer, payee):
    contract.create_escrow(
        args=[str(payee.address), BRIEF, CRITERIA, True, 0],
        value=1000,
    ).transact(account=payer)
    contract.submit_deliverable(args=[0, GOOD_SUBMISSION]).transact(account=payee)
    contract.verify_deliverable(args=[0]).transact(account=payer)
    if contract.get_status(args=[0]).call() != 2:
        pytest.skip("validator consensus did not return APPROVED for this run")

    tx = contract.propose_cancel(args=[0]).transact(account=payer)
    assert tx_execution_failed(tx)


# ---------------------------------------------------------------------
# 7. Disputes / arbiter
# ---------------------------------------------------------------------
def test_dispute_requires_submission(contract, payer, payee):
    contract.create_escrow(
        args=[str(payee.address), BRIEF, CRITERIA, True, 0],
        value=1000,
    ).transact(account=payer)
    # no submission yet — dispute should be rejected
    tx = contract.raise_dispute(args=[0, "no work has even been submitted"]).transact(account=payer)
    assert tx_execution_failed(tx)


def test_dispute_wrong_caller_rejected(contract, payer, payee):
    contract.create_escrow(
        args=[str(payee.address), BRIEF, CRITERIA, True, 0],
        value=1000,
    ).transact(account=payer)
    contract.submit_deliverable(args=[0, GOOD_SUBMISSION]).transact(account=payee)

    tx = contract.raise_dispute(args=[0, "I disagree with the outcome"]).transact(account=payer)
    assert tx_execution_succeeded(tx)
    assert contract.get_status(args=[0]).call() == 6  # DISPUTED
    assert contract.get_disputed_by(args=[0]).call().lower() == str(payer.address).lower()

    # non-arbiter cannot resolve, even the payee/payer themselves
    bad_resolve = contract.resolve_dispute(
        args=[0, True, "trying to self-resolve"]
    ).transact(account=payee)
    assert tx_execution_failed(bad_resolve)


def test_dispute_resolve_to_payee(contract, payer, payee):
    contract.create_escrow(
        args=[str(payee.address), BRIEF, CRITERIA, True, 0],
        value=1000,
    ).transact(account=payer)
    contract.submit_deliverable(args=[0, GOOD_SUBMISSION]).transact(account=payee)
    contract.raise_dispute(args=[0, "criteria was met, verify_deliverable never ran"]).transact(account=payee)
    assert contract.get_status(args=[0]).call() == 6  # DISPUTED

    payee_balance_before = payee.balance
    # owner is arbiter by default
    tx = contract.resolve_dispute(
        args=[0, True, "Reviewed manually; work satisfies the brief."]
    ).transact(account=payer)  # payer is the default/owner account here
    assert tx_execution_succeeded(tx)
    assert contract.get_status(args=[0]).call() == 4  # RELEASED
    assert payee.balance > payee_balance_before
    assert contract.get_dispute_resolution_note(args=[0]).call() != ""


def test_dispute_resolve_to_payer(contract, payer, payee):
    contract.create_escrow(
        args=[str(payee.address), BRIEF, CRITERIA, True, 0],
        value=1000,
    ).transact(account=payer)
    contract.submit_deliverable(args=[0, "low effort submission"]).transact(account=payee)
    contract.raise_dispute(args=[0, "work does not meet the brief at all"]).transact(account=payer)
    assert contract.get_status(args=[0]).call() == 6  # DISPUTED

    payer_balance_before = payer.balance
    tx = contract.resolve_dispute(
        args=[0, False, "Reviewed manually; work does not satisfy the brief."]
    ).transact(account=payer)  # owner/arbiter
    assert tx_execution_succeeded(tx)
    assert contract.get_status(args=[0]).call() == 5  # REFUNDED
    assert payer.balance > payer_balance_before


# ---------------------------------------------------------------------
# 8. Admin controls
# ---------------------------------------------------------------------
def test_admin_controls(contract, payer):
    owner = payer
    non_owner = create_account()

    assert contract.get_owner().call().lower() == str(owner.address).lower()
    assert contract.get_arbiter().call().lower() == str(owner.address).lower()
    assert contract.get_treasury().call().lower() == str(owner.address).lower()
    assert contract.get_fee_bps().call() == 0
    assert contract.get_paused().call() is False

    # non-owner cannot change admin settings
    assert tx_execution_failed(contract.set_fee_bps(args=[100]).transact(account=non_owner))
    assert tx_execution_failed(contract.set_paused(args=[True]).transact(account=non_owner))

    # fee cannot exceed the hard cap
    assert tx_execution_failed(contract.set_fee_bps(args=[1001]).transact(account=owner))
    assert tx_execution_succeeded(contract.set_fee_bps(args=[1000]).transact(account=owner))
    assert contract.get_fee_bps().call() == 1000

    # arbiter/treasury reassignment
    new_arbiter = create_account()
    assert tx_execution_succeeded(
        contract.set_arbiter(args=[str(new_arbiter.address)]).transact(account=owner)
    )
    assert contract.get_arbiter().call().lower() == str(new_arbiter.address).lower()

    assert tx_execution_failed(
        contract.set_treasury(args=[ZERO_ADDRESS]).transact(account=owner)
    )


def test_transfer_ownership(contract, payer):
    owner = payer
    non_owner = create_account()
    new_owner = create_account()

    # non-owner cannot transfer ownership
    assert tx_execution_failed(
        contract.transfer_ownership(args=[str(new_owner.address)]).transact(account=non_owner)
    )
    assert contract.get_owner().call().lower() == str(owner.address).lower()

    # cannot transfer to the zero address
    assert tx_execution_failed(
        contract.transfer_ownership(args=[ZERO_ADDRESS]).transact
