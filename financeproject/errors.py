"""Exceptions with user-facing messages.

The CLI prints the message of any ``FinanceError`` without a traceback.
"""


class FinanceError(Exception):
    """Base class for errors the user can fix."""


class AmountError(FinanceError, ValueError):
    """An amount could not be parsed or has too many decimals."""


class DateError(FinanceError, ValueError):
    """A date could not be parsed."""


class AccountError(FinanceError):
    """Invalid account path, kind or commodity."""


class AccountNotFound(AccountError):
    pass


class AmbiguousAccount(AccountError):
    def __init__(self, query: str, candidates: list[str]):
        self.candidates = candidates
        shown = "\n  ".join(candidates[:10])
        more = f"\n  … and {len(candidates) - 10} more" if len(candidates) > 10 else ""
        super().__init__(f"'{query}' matches more than one account:\n  {shown}{more}")


class LedgerError(FinanceError):
    """A transaction breaks a ledger rule."""


class UnbalancedTransaction(LedgerError):
    pass


class DuplicateTransaction(LedgerError):
    pass


class SourceError(FinanceError):
    """A data source returned an error or unexpected content."""


class EvdsKeyError(SourceError):
    pass
