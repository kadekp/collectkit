"""
Payment Checker Module for verifying payment link status.

Polls the payment URL configured by PAYMENT_LINK_BASE to determine whether
a customer's bill has been paid. Used by the scheduled task processor in
`src/worker.py`.

Detection logic:
- Response body contains `PAID_INDICATOR` (configurable via
  `PAYMENT_PAID_INDICATOR`, default `customer has no bills`) → PAID
- Everything else (error, unpaid, ambiguous) → NEEDS_HUMAN
"""

import os

import requests

from .logging_config import get_logger

logger = get_logger(__name__)

# Configuration (env-driven; defaults work for any provider that returns
# the configured PAID_INDICATOR substring on a "no bills" response)
PAYMENT_URL_BASE = os.getenv(
    "PAYMENT_LINK_BASE",
    "https://example.com/payment-url",
).rstrip("/")
REQUEST_TIMEOUT = int(os.getenv("PAYMENT_CHECK_TIMEOUT_SECONDS", "10"))
PAID_INDICATOR = os.getenv("PAYMENT_PAID_INDICATOR", "customer has no bills").lower()
USER_AGENT = os.getenv("PAYMENT_CHECK_USER_AGENT", "collectkit/1.0")


def get_payment_link_url(customer_number: str) -> str:
    """
    Construct the payment URL for a customer.

    Args:
        customer_number: The customer's ID

    Returns:
        Full payment URL
    """
    return f"{PAYMENT_URL_BASE}/{customer_number}"


def check_payment_status(customer_number: str) -> dict:
    """
    Check if a payment has been made by examining the payment link.

    Uses authoritative API response instead of HTML scraping.
    When customer has no bills, API returns HTTP 400 with "customer has no bills".

    Args:
        customer_number: The customer's ID

    Returns:
        {
            "status": "paid" | "needs_human",
            "raw_response": str (first 500 chars of response for debugging)
        }

    Detection Logic:
    - "customer has no bills" in response → PAID (authoritative signal)
    - Everything else → NEEDS_HUMAN (escalate to CS for manual verification)
    """
    url = get_payment_link_url(customer_number)

    try:
        response = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/json",
            }
        )

        # Log response info for debugging
        logger.debug("Payment check response", extra={
            "customer_number": customer_number,
            "status_code": response.status_code,
        })

        # Get response content
        content = response.text.lower()
        raw_response = response.text[:500]  # First 500 chars for logging

        # Check for authoritative "paid" indicator
        # API returns HTTP 400 with "customer has no bills" when customer has paid
        if PAID_INDICATOR in content:
            return {"status": "paid", "raw_response": raw_response}

        # Everything else → not confirmed as paid
        # This includes: active payment page (still has bill), errors, ambiguous responses
        return {"status": "needs_human", "raw_response": raw_response}

    except requests.Timeout:
        logger.error("Payment check timeout", extra={
            "event": "payment_check_timeout",
            "customer_number": customer_number,
        })
        return {"status": "needs_human", "raw_response": "Request timeout - escalating to CS"}

    except requests.RequestException as e:
        logger.error("Payment check request error", extra={
            "event": "payment_check_request_error",
            "customer_number": customer_number,
            "error": str(e),
        })
        return {"status": "needs_human", "raw_response": f"Request error: {str(e)}"}

    except Exception as e:
        logger.error("Payment check unexpected error", extra={
            "event": "payment_check_error",
            "customer_number": customer_number,
            "error": str(e),
        })
        return {"status": "needs_human", "raw_response": f"Unexpected error: {str(e)}"}
