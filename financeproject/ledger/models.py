"""Database tables.

Amounts and prices are stored as decimal strings (``DecimalText``) so nothing
is ever rounded by a float. A posting's amount is signed: assets and expenses
are positive, liabilities, income and equity negative, and the postings of a
transaction add up to zero in every commodity.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


class DecimalText(TypeDecorator):
    """Exact ``Decimal`` stored as text (SQLite has no decimal type)."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        d = value if isinstance(value, Decimal) else Decimal(str(value))
        if not d.is_finite():
            raise ValueError(f"Cannot store a non-finite number: {value!r}")
        return format(d, "f")

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


class Base(DeclarativeBase):
    pass


class Meta(Base):
    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Commodity(Base):
    """A unit that can be held: a currency, gram gold, a fund, a share…"""

    __tablename__ = "commodities"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20))
    decimals: Mapped[int] = mapped_column(Integer, default=2)
    price_source: Mapped[str] = mapped_column(String(20), default="manual")


class Person(Base):
    __tablename__ = "people"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    note: Mapped[str | None] = mapped_column(Text)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(String(255), unique=True)
    type: Mapped[str] = mapped_column(String(10), index=True)
    kind: Mapped[str | None] = mapped_column(String(30))
    commodity_code: Mapped[str | None] = mapped_column(ForeignKey("commodities.code"))
    """Single commodity of asset and liability accounts; ``None`` = any commodity."""
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"))
    """``None`` means shared by the household."""
    institution: Mapped[str | None] = mapped_column(String(120))
    liquidity_tier: Mapped[int | None] = mapped_column(Integer)
    """Overrides the kind's default tier."""
    opened_on: Mapped[dt.date | None] = mapped_column(Date)
    closed_on: Mapped[dt.date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)

    owner: Mapped[Person | None] = relationship()
    commodity: Mapped[Commodity | None] = relationship()

    def __repr__(self) -> str:
        return f"<Account {self.path}>"


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    description: Mapped[str] = mapped_column(String(500))
    payee: Mapped[str | None] = mapped_column(String(200))
    note: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(80), default="manual")
    import_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    """Hash of an imported row, used to skip duplicates on re-import."""
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.current_timestamp())

    postings: Mapped[list[Posting]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan", order_by="Posting.id"
    )
    installment_plan: Mapped[InstallmentPlan | None] = relationship(
        back_populates="transaction", cascade="all, delete-orphan", uselist=False
    )
    loan_payment: Mapped[LoanPayment | None] = relationship(
        back_populates="transaction", cascade="all, delete-orphan", uselist=False
    )


class Posting(Base):
    __tablename__ = "postings"

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    commodity_code: Mapped[str] = mapped_column(ForeignKey("commodities.code"))
    amount: Mapped[Decimal] = mapped_column(DecimalText(48))
    memo: Mapped[str | None] = mapped_column(String(300))

    transaction: Mapped[Transaction] = relationship(back_populates="postings")
    account: Mapped[Account] = relationship()


class InstallmentPlan(Base):
    """A card purchase paid in equal monthly installments (Turkish "taksit").

    The whole purchase is a debt from the day it is made (the transaction books
    it in full); the plan says in which months the parts fall due.
    """

    __tablename__ = "installment_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), unique=True, index=True
    )
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    """The liability (card) account the installments are owed on."""
    count: Mapped[int] = mapped_column(Integer)
    first_due: Mapped[dt.date] = mapped_column(Date)
    """First day of the month in which the first installment is due."""
    total: Mapped[Decimal] = mapped_column(DecimalText(48))

    transaction: Mapped[Transaction] = relationship(back_populates="installment_plan")
    account: Mapped[Account] = relationship()


class Loan(Base):
    """The terms of a loan whose balance is a liability account.

    The terms give the payment plan; payments are ordinary transactions linked
    through ``LoanPayment``. Rates are fractions: a monthly rate of 3.49 % is
    0.0349, KKDF and BSMV of 15 % are 0.15.
    """

    __tablename__ = "loans"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), unique=True)
    principal: Mapped[Decimal] = mapped_column(DecimalText(48))
    monthly_rate: Mapped[Decimal] = mapped_column(DecimalText(48))
    count: Mapped[int] = mapped_column(Integer)
    first_due: Mapped[dt.date] = mapped_column(Date)
    disbursed: Mapped[dt.date] = mapped_column(Date)
    method: Mapped[str] = mapped_column(String(20), default="annuity")
    kkdf: Mapped[Decimal] = mapped_column(DecimalText(48), default=Decimal(0))
    bsmv: Mapped[Decimal] = mapped_column(DecimalText(48), default=Decimal(0))
    fees: Mapped[Decimal] = mapped_column(DecimalText(48), default=Decimal(0))
    """Paid when the loan is taken (file fee, insurance): part of the annual cost rate."""
    start_number: Mapped[int] = mapped_column(Integer, default=1)
    """First installment kept in this ledger; earlier ones were paid before it."""
    reference: Mapped[str | None] = mapped_column(String(60), unique=True)
    """The bank's number for the loan, as it appears on statements."""
    interest_account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    tax_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    """Where KKDF and BSMV go; ``None``: with the interest."""

    account: Mapped[Account] = relationship(foreign_keys=[account_id])
    interest_account: Mapped[Account] = relationship(foreign_keys=[interest_account_id])
    tax_account: Mapped[Account | None] = relationship(foreign_keys=[tax_account_id])
    payments: Mapped[list[LoanPayment]] = relationship(back_populates="loan", cascade="all, delete-orphan")


class LoanPayment(Base):
    """A transaction that paid a loan, and the installment it paid (if it was one)."""

    __tablename__ = "loan_payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    loan_id: Mapped[int] = mapped_column(ForeignKey("loans.id", ondelete="CASCADE"), index=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id", ondelete="CASCADE"), unique=True)
    number: Mapped[int | None] = mapped_column(Integer)

    loan: Mapped[Loan] = relationship(back_populates="payments")
    transaction: Mapped[Transaction] = relationship(back_populates="loan_payment")


class Price(Base):
    """One unit of ``commodity_code`` cost ``value`` units of ``quote_code`` on ``date``."""

    __tablename__ = "prices"
    __table_args__ = (UniqueConstraint("commodity_code", "quote_code", "date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    commodity_code: Mapped[str] = mapped_column(ForeignKey("commodities.code"), index=True)
    quote_code: Mapped[str] = mapped_column(ForeignKey("commodities.code"))
    date: Mapped[dt.date] = mapped_column(Date)
    value: Mapped[Decimal] = mapped_column(DecimalText(48))
    source: Mapped[str] = mapped_column(String(40))
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.current_timestamp())


class IndexValue(Base):
    """Monthly index level, e.g. the CPI (``series="CPI"``)."""

    __tablename__ = "index_values"
    __table_args__ = (UniqueConstraint("series", "period"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    series: Mapped[str] = mapped_column(String(40))
    period: Mapped[dt.date] = mapped_column(Date)
    """First day of the month."""
    value: Mapped[Decimal] = mapped_column(DecimalText(48))
    source: Mapped[str] = mapped_column(String(40))
    base: Mapped[str | None] = mapped_column(String(40))
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.current_timestamp())


class Rule(Base):
    """Categorization rule: a description that matches goes to ``account``."""

    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    pattern: Mapped[str] = mapped_column(String(200))
    is_regex: Mapped[bool] = mapped_column(Boolean, default=False)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    priority: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.current_timestamp())

    account: Mapped[Account] = relationship()
