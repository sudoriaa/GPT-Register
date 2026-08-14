# -*- coding: utf-8 -*-
"""OAICS checkout capability detection settings."""
from config.env_loader import apply_env_overrides


# Detection is read-only: it creates a checkout session to identify the
# processor/capabilities, then stops before any payment or confirmation call.
OAICS_AUTO_CHECK = True
OAICS_TIMEOUT = 20.0
OAICS_MAX_ATTEMPTS = 4
OAICS_RETRY_DELAY = 1.5
OAICS_BILLING_COUNTRY = "US"
OAICS_CURRENCY = "USD"
OAICS_EXPECTED_METHOD = "paypal"
OAICS_PLAN_NAME = "chatgptplusplan"
OAICS_PROMO_CAMPAIGN = "plus-1-month-free"
OAICS_INCLUDE_PROMO = True


apply_env_overrides(globals(), {
    "OAICS_AUTO_CHECK": "bool",
    "OAICS_TIMEOUT": "float",
    "OAICS_MAX_ATTEMPTS": "int",
    "OAICS_RETRY_DELAY": "float",
    "OAICS_BILLING_COUNTRY": "str",
    "OAICS_CURRENCY": "str",
    "OAICS_EXPECTED_METHOD": "str",
    "OAICS_PLAN_NAME": "str",
    "OAICS_PROMO_CAMPAIGN": "str",
    "OAICS_INCLUDE_PROMO": "bool",
})
