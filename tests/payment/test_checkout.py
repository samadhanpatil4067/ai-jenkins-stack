"""
tests/payment/test_checkout.py
================================
Payment service checkout tests — demo file for ARIA Stack episode.

Includes:
  - Standard unit tests (always pass)
  - test_checkout_timing_sensitive — the FLAKY TEST used in the demo
    This test fails ~60% of the time due to random timing.
    When pushed, the AI Stage Gate classifies it as FLAKY_TEST at 0.91
    confidence and schedules an isolated retry — nobody gets paged.

Run all:
  pytest tests/payment/ -v --tb=short

Run just the flaky test:
  pytest tests/payment/test_checkout.py::test_checkout_timing_sensitive -v
"""

import random
import time
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest


# ── Mocked dependencies ───────────────────────────────────────────
class PaymentGateway:
    def charge(self, amount: Decimal, card_token: str) -> dict:
        return {"status": "success", "charge_id": "ch_test_001", "amount": float(amount)}

    def refund(self, charge_id: str) -> dict:
        return {"status": "refunded", "charge_id": charge_id}


class OrderService:
    def get_order(self, order_id: str) -> dict:
        return {
            "id": order_id,
            "items": [{"sku": "ITEM-001", "qty": 2, "price": 29.99}],
            "total": Decimal("59.98"),
            "status": "pending",
        }

    def update_status(self, order_id: str, status: str) -> bool:
        return True


# ── Standard tests (always pass) ─────────────────────────────────
class TestCheckoutBasic:
    """Standard checkout flow — deterministic, always pass."""

    def setup_method(self):
        self.gateway = PaymentGateway()
        self.orders  = OrderService()

    def test_successful_checkout(self):
        """Happy path: valid card + in-stock order → success."""
        order  = self.orders.get_order("ORD-001")
        result = self.gateway.charge(order["total"], "tok_visa_test")
        assert result["status"] == "success"
        assert result["amount"] == pytest.approx(59.98)

    def test_checkout_calculates_total_correctly(self):
        """Order total should sum item prices correctly."""
        order = self.orders.get_order("ORD-001")
        expected_total = sum(
            item["qty"] * item["price"] for item in order["items"]
        )
        assert float(order["total"]) == pytest.approx(expected_total)

    def test_refund_after_failed_delivery(self):
        """Refund should succeed after a failed delivery attempt."""
        order   = self.orders.get_order("ORD-002")
        charge  = self.gateway.charge(order["total"], "tok_visa_test")
        refund  = self.gateway.refund(charge["charge_id"])
        assert refund["status"] == "refunded"

    def test_zero_amount_rejected(self):
        """Zero-value charges should be rejected before hitting gateway."""
        with pytest.raises((ValueError, AssertionError)):
            amount = Decimal("0.00")
            assert amount > 0, "Cannot charge zero amount"

    def test_order_status_updated_on_success(self):
        """Order status must move to 'paid' after successful charge."""
        updated = self.orders.update_status("ORD-001", "paid")
        assert updated is True

    @patch("tests.payment.test_checkout.PaymentGateway.charge")
    def test_gateway_timeout_handled_gracefully(self, mock_charge):
        """Gateway timeout should raise a controlled exception, not crash."""
        mock_charge.side_effect = TimeoutError("Gateway timeout after 30s")
        gw = PaymentGateway()
        with pytest.raises(TimeoutError, match="Gateway timeout"):
            gw.charge(Decimal("29.99"), "tok_visa_test")

    def test_decimal_precision_no_floating_point_error(self):
        """Financial amounts must use Decimal, not float, to avoid rounding errors."""
        price1 = Decimal("0.10")
        price2 = Decimal("0.20")
        total  = price1 + price2
        assert total == Decimal("0.30")           # Decimal: exact
        assert 0.10 + 0.20 != 0.30               # float: floating-point error!

    def test_duplicate_charge_prevention(self):
        """Same idempotency key should not trigger a second charge."""
        idempotency_key = "order-ORD-001-attempt-1"
        # In a real implementation this checks a database — mocked here
        seen_keys = {idempotency_key}
        assert idempotency_key in seen_keys  # already processed


# ── Integration-style tests ───────────────────────────────────────
class TestCheckoutIntegration:
    """Slightly heavier tests — mock external services."""

    def test_full_checkout_flow(self):
        """End-to-end checkout: fetch order → charge → update status."""
        orders  = OrderService()
        gateway = PaymentGateway()

        order   = orders.get_order("ORD-010")
        charge  = gateway.charge(order["total"], "tok_mastercard_test")
        updated = orders.update_status(order["id"], "paid")

        assert charge["status"]  == "success"
        assert updated           is True

    def test_partial_refund(self):
        """Partial refund should only refund the specified amount."""
        gateway   = PaymentGateway()
        charge    = gateway.charge(Decimal("100.00"), "tok_visa_test")
        # In real code: gateway.partial_refund(charge_id, Decimal("30.00"))
        # Mocked for demo:
        assert charge["amount"] == pytest.approx(100.0)


# ── THE FLAKY TEST ─────────────────────────────────────────────────
def test_checkout_timing_sensitive():
    """
    ⚠ DEMO: This test is intentionally flaky.

    It simulates a real-world race condition where a timing-sensitive
    operation fails non-deterministically — ~60% failure rate.

    What happens in the ARIA demo:
    1. This test is pushed in a commit
    2. It fails on the first Jenkins run
    3. AI Sidecar analyses the logs → FLAKY_TEST at 0.91 confidence
    4. Isolated retry is scheduled automatically
    5. GitHub issue #847 is filed with the diagnosis
    6. Nobody gets paged

    In production code, fix this by:
      - Using a fixed timeout instead of random sleep
      - Adding a retry decorator: @pytest.mark.flaky(reruns=3)
      - Mocking the external timing dependency
    """
    # Simulate a race condition / timing-sensitive external call
    latency = random.uniform(0.05, 0.95)
    time.sleep(latency)

    # This assertion fails when the "external service" is slow
    # Real-world equivalent: response time SLA check, lock acquisition, etc.
    threshold = 0.50
    assert latency < threshold, (
        f"Checkout timing check failed: "
        f"response_time={latency:.3f}s exceeded threshold={threshold}s. "
        f"This is a race condition — not a real bug."
    )


# ── Parametrized edge cases ────────────────────────────────────────
@pytest.mark.parametrize("amount,expected", [
    (Decimal("9.99"),   True),
    (Decimal("999.99"), True),
    (Decimal("0.01"),   True),
    (Decimal("0.00"),   False),
    (Decimal("-1.00"),  False),
])
def test_amount_validation(amount, expected):
    """Valid amounts are positive and non-zero."""
    is_valid = amount > Decimal("0")
    assert is_valid == expected


if __name__ == "__main__":
    # Run with: python tests/payment/test_checkout.py
    pytest.main([__file__, "-v", "--tb=short"])
