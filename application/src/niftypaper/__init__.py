"""NIFTY options paper-trading system, version 0.0.1 (scaffold V6-0.1).

Simulated trading only. This package contains no broker integration, no order-placement code and no network code.
"""
from .eligibility import (EligibilityPolicy, EligibilityResult, ExecutableQuote, IneligibleQuoteError, Reason, Side,
                          check_fill_eligibility, fill_price, require_fillable)
from .quotes import Quote, QuoteType

__version__ = "0.0.1"

__all__ = ["EligibilityPolicy", "EligibilityResult", "ExecutableQuote", "IneligibleQuoteError", "Quote", "QuoteType", "Reason", "Side",
           "check_fill_eligibility", "fill_price", "require_fillable", "__version__"]
