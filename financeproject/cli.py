"""Command line interface. Run ``finance --help``.

The CLI is the prototype's user interface; all logic lives in the library
modules, so a web interface can reuse it later.
"""

from __future__ import annotations

import functools
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Optional

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from financeproject import VERSION_LABEL, __version__, config
from financeproject.core.dates import (
    FREQUENCIES,
    add_months,
    month_label,
    month_start,
    parse_date,
)
from financeproject.core.inflation import Deflator, annual_pct, monthly_pct
from financeproject.core.kinds import (
    ASSET,
    EQUITY,
    EXPENSE,
    INCOME,
    KINDS,
    LIABILITY,
    LONG,
    TERMS,
    TIERS,
    debt_term,
)
from financeproject.core.money import (
    DEFAULT_NUMBER_FORMAT,
    NUMBER_FORMATS,
    format_amount,
    parse_decimal,
    set_number_format,
)
from financeproject.core.prices import PriceBook
from financeproject.errors import FinanceError
from financeproject.ledger import accounts as acc_mod
from financeproject.ledger.db import (
    backup,
    init_db,
    is_initialised,
    make_engine,
    open_ledger,
    session_scope,
)
from financeproject.ledger.models import (
    Account,
    Commodity,
    Person,
    Posting,
    Price,
    Rule,
    Transaction,
)
from financeproject.ledger.queries import (
    account_balance,
    commodity_totals,
    transaction_date_range,
)
from financeproject.ledger.transactions import (
    Leg,
    add_transaction,
    delete_transaction,
    opening_balance,
    record_exchange,
    record_expense,
    record_income,
    record_transfer,
)
from financeproject.prices.store import (
    MANUAL,
    index_sources,
    load_index,
    load_pricebook,
    price_range,
    upsert_price,
)

# emoji=False: account paths such as 'Assets:Bank:Checking' contain emoji codes (':bank:').
console = Console(emoji=False)
err = Console(stderr=True, emoji=False)

app = typer.Typer(
    name="finance",
    help=f"financeproject {VERSION_LABEL}: a double-entry household ledger with net worth in four units "
    "(TRY, real TRY, USD, grams of gold).",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,  # plain help text: no markup, and no emoji codes such as ":bank:"
)
account_app = typer.Typer(help="Add, list and close accounts.", no_args_is_help=True)
txn_app = typer.Typer(help="Add, list and delete general transactions.", no_args_is_help=True)
rule_app = typer.Typer(help="Categorization rules (description contains → account).", no_args_is_help=True)
price_app = typer.Typer(help="Exchange rates, gold, CPI and prices entered by hand.", no_args_is_help=True)
commodity_app = typer.Typer(help="Commodities: currencies, gold, funds, stocks.", no_args_is_help=True)
person_app = typer.Typer(help="Account owners (household members).", no_args_is_help=True)
config_app = typer.Typer(help="Settings: EVDS key, number format.", no_args_is_help=True)
loan_app = typer.Typer(help="Loans: terms, payment plans, payments.", no_args_is_help=True)
for sub, name in (
    (account_app, "account"),
    (txn_app, "txn"),
    (rule_app, "rule"),
    (price_app, "price"),
    (commodity_app, "commodity"),
    (person_app, "person"),
    (config_app, "config"),
    (loan_app, "loan"),
):
    app.add_typer(sub, name=name)


@dataclass
class State:
    db_path: Path
    explicit: bool


def guarded(fn):
    """Print user errors without a traceback and exit with status 1."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except FinanceError as exc:
            err.print(f"[bold red]Error:[/] {escape(str(exc))}")
            raise typer.Exit(1) from None
        except httpx.HTTPError as exc:
            err.print(f"[bold red]Network error:[/] {escape(str(exc))}")
            raise typer.Exit(2) from None

    return wrapper


@app.callback()
def _root(
    ctx: typer.Context,
    db: Annotated[
        Optional[Path],
        typer.Option(
            "--db",
            "-d",
            help=f"Ledger file. Default: ${config.ENV_DB}, or ledger.sqlite in the user data directory "
            "(see 'finance config show').",
        ),
    ] = None,
) -> None:
    try:
        set_number_format(config.number_format() or DEFAULT_NUMBER_FORMAT)
    except FinanceError as exc:
        err.print(f"[bold red]Error:[/] {escape(str(exc))}")
        raise typer.Exit(1) from None
    ctx.obj = State(db.expanduser() if db else config.default_db_path(), db is not None)


@contextmanager
def ledger(ctx: typer.Context) -> Iterator[Session]:
    path: Path = ctx.obj.db_path
    if not path.exists():
        raise FinanceError(f"No ledger at {path}\nCreate one first: finance init")
    engine = make_engine(path)
    try:
        if not is_initialised(engine):
            raise FinanceError(f"{path} is not a financeproject ledger.")
        open_ledger(engine)
        with session_scope(engine) as session:
            yield session
    finally:
        engine.dispose()


def table(*columns: str, **kwargs) -> Table:
    """Rich table; a column name ending in '>' is right-aligned (numbers)."""
    t = Table(**kwargs)
    for name in columns:
        if name.endswith(">"):
            t.add_column(name[:-1], justify="right")
        else:
            t.add_column(name)
    return t


def money(value: Decimal | float | None, decimals: int = 2) -> str:
    return format_amount(value, decimals)


def amount(text: str) -> Decimal:
    return parse_decimal(text)


def day(text: str | None) -> date:
    return parse_date(text or "today")


def _decimals(session: Session, code: str) -> int:
    c = session.get(Commodity, code)
    return c.decimals if c else 2


def _check_choice(value: str, choices: tuple[str, ...], option: str) -> str:
    if value not in choices:
        raise FinanceError(f"{option} must be one of: {', '.join(choices)}")
    return value


# --------------------------------------------------------------------------- setup


@app.command()
@guarded
def init(ctx: typer.Context) -> None:
    """Create the ledger (an existing one is left as it is) and load the bundled CPI."""
    from financeproject.prices.update import load_bundled_cpi

    path: Path = ctx.obj.db_path
    existed = path.exists()
    engine = make_engine(path)
    try:
        if existed and is_initialised(engine):
            open_ledger(engine)
        init_db(engine)
        with session_scope(engine) as s:
            report = load_bundled_cpi(s)
    finally:
        engine.dispose()
    path.chmod(0o600)
    console.print(f"[green]Ledger {'already existed' if existed else 'created'}:[/] {path}")
    for line in report.messages:
        console.print(f"  {line}")
    console.print(
        "\nNext steps:\n"
        '  finance account add "Assets:Bank:Checking" --kind checking --institution "Bank name"\n'
        '  finance opening "Assets:Bank:Checking" 25000\n'
        "  finance price update\n"
        "  finance balance-sheet\n"
        "To try a made-up household: finance demo"
    )


@app.command()
@guarded
def status(ctx: typer.Context) -> None:
    """Summary of the ledger: file, counts, latest prices, CPI, consistency."""
    with ledger(ctx) as s:
        n_acc = s.scalar(select(func.count()).select_from(Account))
        n_tx = s.scalar(select(func.count()).select_from(Transaction))
        n_price = s.scalar(select(func.count()).select_from(Price))
        first, last = transaction_date_range(s)
        t = Table(show_header=False, box=None, pad_edge=False)
        t.add_row("Version", f"{VERSION_LABEL} ({__version__})")
        t.add_row("Ledger", str(ctx.obj.db_path))
        t.add_row("Accounts / transactions / prices", f"{n_acc} / {n_tx} / {n_price}")
        t.add_row("Transaction dates", f"{first or '—'} → {last or '—'}")
        for code, quote in (("USD", "TRY"), ("EUR", "TRY"), ("XAU_G", "USD")):
            a, b = price_range(s, code, quote)
            t.add_row(f"Prices {code}/{quote}", f"{a or '—'} → {b or '—'}")
        index = load_index(s)
        sources = ", ".join(f"{k}: {v} months" for k, v in index_sources(s).items()) or "none"
        if index:
            t.add_row("CPI", f"{min(index):%Y-%m} → {max(index):%Y-%m} ({sources})")
        else:
            t.add_row("CPI", "none")
        t.add_row("EVDS key", "set" if config.evds_api_key() else "not set (the TCMB table is used)")
        imbalance = {c: v for c, v in commodity_totals(s).items() if v != 0}
        t.add_row(
            "Ledger balance",
            "[green]ok (every commodity sums to zero)[/]" if not imbalance else f"[red]ERROR: {imbalance}[/]",
        )
        console.print(t)


# --------------------------------------------------------------------------- commodities, people, settings


@commodity_app.command("list")
@guarded
def commodity_list(ctx: typer.Context) -> None:
    """Defined commodities."""
    with ledger(ctx) as s:
        t = Table("Code", "Name", "Kind", "Decimals", "Price source")
        for c in s.scalars(select(Commodity).order_by(Commodity.code)):
            t.add_row(c.code, escape(c.name), c.kind, str(c.decimals), c.price_source)
        console.print(t)


COMMODITY_KINDS = ("currency", "gold", "fund", "stock", "other")


@commodity_app.command("add")
@guarded
def commodity_add(
    ctx: typer.Context,
    code: Annotated[str, typer.Argument(help="For example GBP, TEFAS.AFT, BIST.THYAO")],
    name: Annotated[str, typer.Argument(help="Display name")],
    kind: Annotated[str, typer.Option(help=", ".join(COMMODITY_KINDS))] = "other",
    decimals: Annotated[int, typer.Option(help="Decimal places of quantities")] = 6,
    price_source: Annotated[str, typer.Option(help="tcmb (currencies published by TCMB) or manual")] = MANUAL,
) -> None:
    """Add a commodity. For a currency published by TCMB: --kind currency --price-source tcmb --decimals 2."""
    import re

    code = code.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_.-]{0,31}", code):
        raise FinanceError("A code starts with a letter and has letters, digits, '_', '.' or '-' (at most 32).")
    _check_choice(kind, COMMODITY_KINDS, "--kind")
    _check_choice(price_source, ("tcmb", MANUAL), "--price-source")
    if not 0 <= decimals <= 8:
        raise FinanceError("--decimals must be between 0 and 8.")
    with ledger(ctx) as s:
        if s.get(Commodity, code):
            raise FinanceError(f"{code} already exists.")
        s.add(Commodity(code=code, name=name, kind=kind, decimals=decimals, price_source=price_source))
    console.print(f"[green]Commodity added:[/] {code} ({escape(name)})")


@person_app.command("list")
@guarded
def person_list(ctx: typer.Context) -> None:
    """Account owners. Accounts without an owner are shared."""
    with ledger(ctx) as s:
        t = table("Name", "Accounts>")
        for p in s.scalars(select(Person).order_by(Person.name)):
            n = s.scalar(select(func.count()).select_from(Account).where(Account.owner_id == p.id))
            t.add_row(escape(p.name), str(n))
        console.print(t)


@person_app.command("add")
@guarded
def person_add(ctx: typer.Context, name: str) -> None:
    """Add a person (also added automatically by 'account add --owner')."""
    with ledger(ctx) as s:
        if acc_mod.get_person(s, name):
            raise FinanceError(f"{name} already exists.")
        acc_mod.get_person(s, name, create=True)
    console.print(f"[green]Person added:[/] {escape(name)}")


@config_app.command("evds-key")
@guarded
def config_evds_key(key: Annotated[str, typer.Argument(help="The API key on your evds3.tcmb.gov.tr profile page")]) -> None:
    """Save the EVDS API key in the settings file (readable only by you)."""
    path = config.save_evds_api_key(key)
    console.print(f"[green]Saved:[/] {path}")


@config_app.command("number-format")
@guarded
def config_number_format(
    style: Annotated[str, typer.Argument(help=" or ".join(f"'{k}'" for k in NUMBER_FORMATS))],
) -> None:
    """How amounts are written: '1,234.56' (default) or '1.234,56'."""
    set_number_format(style)
    path = config.save_setting("display", "number_format", style)
    console.print(f"[green]Saved:[/] {path} (numbers now look like {format_amount(Decimal('1234567.89'))})")


@config_app.command("show")
@guarded
def config_show(ctx: typer.Context) -> None:
    """File locations and settings."""
    key = config.evds_api_key()
    console.print(f"Data directory : {config.data_dir()}")
    console.print(f"Ledger         : {ctx.obj.db_path}")
    console.print(f"Settings file  : {config.settings_path()}")
    console.print(f"EVDS key       : {'…' + key[-4:] if key else 'not set'}")
    console.print(f"Number format  : {config.number_format() or DEFAULT_NUMBER_FORMAT}")


# --------------------------------------------------------------------------- accounts


@account_app.command("kinds")
def account_kinds() -> None:
    """Kinds of asset and liability accounts, with liquidity tiers and debt terms."""
    t = Table("Kind", "Description", "Type", "Liquidity / term", "Default commodity")
    for k in KINDS.values():
        where = "Asset" if k.type == ASSET else "Liability"
        extra = TIERS[k.tier] if k.type == ASSET else TERMS[k.term or LONG]
        t.add_row(k.key, k.label, where, extra, k.default_commodity or "— (must be given)")
    console.print(t)
    console.print("Liquidity tiers: " + ", ".join(f"{i} = {label}" for i, label in TIERS.items()))


@account_app.command("add")
@guarded
def account_add(
    ctx: typer.Context,
    path: Annotated[
        str, typer.Argument(help="For example 'Assets:Bank:Checking', 'Liabilities:Credit Card:Bonus', 'Expenses:Food:Groceries'")
    ],
    kind: Annotated[Optional[str], typer.Option(help="Asset or liability kind; see: finance account kinds")] = None,
    commodity: Annotated[Optional[str], typer.Option(help="TRY, USD, XAU_G… (default from the kind)")] = None,
    owner: Annotated[Optional[str], typer.Option(help="A person's name; empty or 'shared' = shared by the household")] = None,
    institution: Annotated[Optional[str], typer.Option(help="Bank or institution")] = None,
    tier: Annotated[Optional[int], typer.Option(help="Liquidity tier (0–3) instead of the kind's default")] = None,
    opened: Annotated[Optional[str], typer.Option(help="No transactions before this date")] = None,
    note: Annotated[Optional[str], typer.Option(help="Note")] = None,
) -> None:
    """Add an account. Roots: Assets, Liabilities, Equity, Income, Expenses."""
    with ledger(ctx) as s:
        a = acc_mod.create_account(
            s,
            path,
            kind=kind,
            commodity=commodity,
            owner=owner,
            institution=institution,
            tier=tier,
            opened_on=parse_date(opened) if opened else None,
            note=note,
        )
        detail = ", ".join(
            filter(None, [a.kind, a.commodity_code, acc_mod.owner_name(a) if a.type in (ASSET, LIABILITY) else None])
        )
    console.print(f"[green]Account added:[/] {escape(a.path)}" + (f" ({escape(detail)})" if detail else ""))


@account_app.command("list")
@guarded
def account_list(
    ctx: typer.Context,
    on: Annotated[Optional[str], typer.Option("--date", help="Date of the balances (default today)")] = None,
    closed: Annotated[bool, typer.Option(help="Show closed accounts too")] = False,
) -> None:
    """Accounts and their balances (natural sign: debts and income are shown positive)."""
    at = day(on)
    with ledger(ctx) as s:
        t = table("Account", "Kind", "Commodity", "Owner", "Institution", "Balance>")
        for a in s.scalars(select(Account).options(selectinload(Account.owner)).order_by(Account.type, Account.path)):
            if a.closed_on and not closed:
                continue
            balances = account_balance(s, a, at)
            sign = -1 if a.type in (LIABILITY, INCOME, EQUITY) else 1
            text = ", ".join(f"{money(sign * v, _decimals(s, c))} {c}" for c, v in sorted(balances.items())) or "0"
            t.add_row(
                escape(a.path) + (" [dim](closed)[/]" if a.closed_on else ""),
                a.kind or a.type,
                a.commodity_code or "any",
                escape(acc_mod.owner_name(a)) if a.type in (ASSET, LIABILITY) else "",
                escape(a.institution or ""),
                text,
            )
        console.print(t)


@account_app.command("close")
@guarded
def account_close(
    ctx: typer.Context,
    account: str,
    on: Annotated[Optional[str], typer.Option("--date", help="Closing date (default today)")] = None,
) -> None:
    """Close an account; its balance must be zero."""
    at = day(on)
    with ledger(ctx) as s:
        a = acc_mod.find_account(s, account)
        if account_balance(s, a, at):
            raise FinanceError(f"The balance of {a.path} is not zero; move it to another account first.")
        later = s.scalar(
            select(func.count()).select_from(Posting).join(Transaction).where(Posting.account_id == a.id, Transaction.date > at)
        )
        if later:
            raise FinanceError(f"{a.path} has {later} postings after {at}.")
        a.closed_on = at
        path = a.path
    console.print(f"[green]Closed:[/] {escape(path)} ({at})")


# --------------------------------------------------------------------------- transactions


DateOption = Annotated[Optional[str], typer.Option("--date", help="Default: today")]


@app.command()
@guarded
def opening(
    ctx: typer.Context,
    account: str,
    balance: Annotated[str, typer.Argument(help="Natural sign: what you hold for an asset, what you owe (positive) for a debt")],
    on: DateOption = None,
) -> None:
    """Enter the starting balance of an account (against Equity:Opening Balances)."""
    with ledger(ctx) as s:
        tx = opening_balance(s, account, amount(balance), day(on))
        console.print(f"[green]#{tx.id}[/] {tx.date} {escape(tx.description)}")


@app.command()
@guarded
def expense(
    ctx: typer.Context,
    value: Annotated[str, typer.Argument(metavar="AMOUNT")],
    expense_account: Annotated[
        str, typer.Argument(metavar="EXPENSE", help="Expense account, e.g. 'Expenses:Food:Groceries' or just 'Groceries'")
    ],
    from_: Annotated[str, typer.Option("--from", help="Account paid from (bank, cash or credit card)")],
    description: Annotated[Optional[str], typer.Option(help="Default: the name of the expense account")] = None,
    on: DateOption = None,
    payee: Annotated[Optional[str], typer.Option(help="Shop or person")] = None,
    installments: Annotated[
        Optional[int], typer.Option(help="Pay in this many monthly installments (credit cards only)")
    ] = None,
    first_due: Annotated[
        Optional[str], typer.Option(help="Month of the first installment, e.g. 2026-11 (default: the next month)")
    ] = None,
) -> None:
    """Spending: the expense grows, the paying account shrinks (on a credit card the debt grows).

    With --installments the whole purchase is still a debt from today; the
    installment plan records in which months its parts fall due.
    """
    from financeproject.core.dates import parse_month
    from financeproject.ledger.installments import record_installment_purchase

    with ledger(ctx) as s:
        e = acc_mod.find_account(s, expense_account)
        text = description or e.path.split(":")[-1]
        if installments is not None:
            tx = record_installment_purchase(
                s, amount(value), e, from_, day(on), text, installments, parse_month(first_due) if first_due else None, payee
            )
            plan = tx.installment_plan
            console.print(
                f"[green]#{tx.id}[/] {tx.date} {escape(tx.description)}: {money(amount(value))} in "
                f"{plan.count} installments from {plan.first_due:%Y-%m}"
            )
            return
        if first_due:
            raise FinanceError("--first-due needs --installments.")
        tx = record_expense(s, amount(value), e, from_, day(on), text, payee)
        console.print(f"[green]#{tx.id}[/] {tx.date} {escape(tx.description)}: {money(amount(value))}")


@app.command()
@guarded
def income(
    ctx: typer.Context,
    value: Annotated[str, typer.Argument(metavar="AMOUNT")],
    income_account: Annotated[str, typer.Argument(metavar="INCOME", help="Income account, e.g. 'Income:Salary'")],
    to: Annotated[str, typer.Option(help="Account the money goes into")],
    description: Annotated[Optional[str], typer.Option()] = None,
    on: DateOption = None,
    payee: Annotated[Optional[str], typer.Option()] = None,
) -> None:
    """Income: the receiving account grows."""
    with ledger(ctx) as s:
        i = acc_mod.find_account(s, income_account)
        tx = record_income(s, amount(value), i, to, day(on), description or i.path.split(":")[-1], payee)
        console.print(f"[green]#{tx.id}[/] {tx.date} {escape(tx.description)}: {money(amount(value))}")


@app.command()
@guarded
def transfer(
    ctx: typer.Context,
    value: Annotated[str, typer.Argument(metavar="AMOUNT")],
    from_: Annotated[str, typer.Option("--from")],
    to: Annotated[str, typer.Option()],
    description: Annotated[Optional[str], typer.Option()] = None,
    on: DateOption = None,
) -> None:
    """Move money between two accounts of the same unit (paying a card bill is a transfer)."""
    with ledger(ctx) as s:
        tx = record_transfer(s, amount(value), from_, to, day(on), description)
        console.print(f"[green]#{tx.id}[/] {tx.date} {escape(tx.description)}: {money(amount(value))}")


@app.command()
@guarded
def exchange(
    ctx: typer.Context,
    from_: Annotated[str, typer.Option("--from", help="Account the payment leaves")],
    paid: Annotated[str, typer.Option(help="Amount leaving the source account")],
    to: Annotated[str, typer.Option(help="Account receiving the other unit")],
    received: Annotated[str, typer.Option(help="Amount received (USD, grams…)")],
    description: Annotated[Optional[str], typer.Option()] = None,
    on: DateOption = None,
) -> None:
    """Buy or sell currency, gold or funds: an exchange between units (through trading accounts)."""
    with ledger(ctx) as s:
        tx = record_exchange(s, from_, amount(paid), to, amount(received), day(on), description)
        console.print(f"[green]#{tx.id}[/] {tx.date} {escape(tx.description)} — {tx.note}")


def _parse_leg(text: str) -> Leg:
    """``"Expenses:Groceries=450"``, ``"USD Account=100 USD"`` or ``"Bank"`` (balancing leg)."""
    if "=" not in text:
        return Leg(text.strip(), None)
    account, rest = text.rsplit("=", 1)
    parts = rest.split()
    if not parts:
        return Leg(account.strip(), None)
    if len(parts) > 2:
        raise FinanceError(f"Cannot read the posting {text!r} (format: ACCOUNT=AMOUNT [UNIT])")
    return Leg(account.strip(), parse_decimal(parts[0]), parts[1].upper() if len(parts) == 2 else None)


@txn_app.command("add")
@guarded
def txn_add(
    ctx: typer.Context,
    description: Annotated[str, typer.Option(help="Description of the transaction")],
    posting: Annotated[
        list[str], typer.Option("--posting", "-p", help="ACCOUNT=AMOUNT [UNIT]; one posting without an amount takes the balance")
    ],
    on: DateOption = None,
    payee: Annotated[Optional[str], typer.Option()] = None,
    note: Annotated[Optional[str], typer.Option()] = None,
) -> None:
    """General transaction: signed amounts that add up to zero in every unit.

    Example: finance txn add --description "Loan payment" -p "Consumer Loan=3000"
    -p "Expenses:Interest and Fees=1500" -p "Checking"
    """
    with ledger(ctx) as s:
        tx = add_transaction(s, day(on), description, [_parse_leg(x) for x in posting], payee=payee, note=note)
        console.print(f"[green]#{tx.id}[/] {tx.date} {escape(tx.description)}")
        for p in tx.postings:
            console.print(
                f"   {escape(p.account.path):<45} {money(p.amount, _decimals(s, p.commodity_code)):>18} {p.commodity_code}"
            )


@txn_app.command("list")
@guarded
def txn_list(
    ctx: typer.Context,
    account: Annotated[Optional[str], typer.Option(help="Only transactions of this account")] = None,
    start: Annotated[Optional[str], typer.Option()] = None,
    end: Annotated[Optional[str], typer.Option()] = None,
    last: Annotated[int, typer.Option(help="At most this many transactions (the newest)")] = 30,
) -> None:
    """List transactions with their postings."""
    with ledger(ctx) as s:
        stmt = select(Transaction).options(selectinload(Transaction.postings).selectinload(Posting.account))
        if account:
            a = acc_mod.find_account(s, account)
            stmt = stmt.where(Transaction.postings.any(Posting.account_id == a.id))
        if start:
            stmt = stmt.where(Transaction.date >= parse_date(start))
        if end:
            stmt = stmt.where(Transaction.date <= parse_date(end))
        txs = list(s.scalars(stmt.order_by(Transaction.date.desc(), Transaction.id.desc()).limit(last)))
        t = table("#>", "Date", "Description", "Account", "Amount>", "Unit", "Source")
        for tx in reversed(txs):
            for i, p in enumerate(tx.postings):
                t.add_row(
                    str(tx.id) if i == 0 else "",
                    str(tx.date) if i == 0 else "",
                    escape(tx.description) if i == 0 else "",
                    escape(p.account.path),
                    money(p.amount, _decimals(s, p.commodity_code)),
                    p.commodity_code,
                    escape(tx.source) if i == 0 else "",
                )
        console.print(t)


@txn_app.command("delete")
@guarded
def txn_delete(ctx: typer.Context, number: Annotated[int, typer.Argument(metavar="ID")]) -> None:
    """Delete a transaction (cannot be undone; 'finance backup' first is a good idea)."""
    with ledger(ctx) as s:
        tx = delete_transaction(s, number)
        console.print(f"[yellow]Deleted:[/] #{number} {tx.date} {escape(tx.description)}")


# --------------------------------------------------------------------------- import and rules


@txn_app.command("recategorize")
@guarded
def txn_recategorize(
    ctx: typer.Context,
    number: Annotated[int, typer.Argument(metavar="ID")],
    account: Annotated[str, typer.Argument(help="The new income or expense account")],
) -> None:
    """Move a simple transaction to another category. The learned model learns from it."""
    from financeproject.ledger.transactions import recategorize

    with ledger(ctx) as s:
        tx = recategorize(s, number, account)
        category = next(p.account.path for p in tx.postings if p.account.type in (INCOME, EXPENSE))
        console.print(f"[green]#{tx.id}[/] {tx.date} {escape(tx.description)} → {escape(category)}")


# --------------------------------------------------------------------------- import and rules


def _print_import(result, dry_run: bool) -> None:
    t = table("Line>", "Date", "Description", "Amount>", "Account", "Counter account", "How", "")
    for r in result.rows[:200]:
        t.add_row(
            str(r.line),
            str(r.date),
            escape((r.payee or r.description)[:40]),
            f"{money(r.amount)} {r.commodity}",
            escape(r.account),
            escape(r.counter_account),
            r.how,
            "[dim]already in[/]" if r.duplicate else "",
        )
    console.print(t)
    if len(result.rows) > 200:
        console.print(f"… {len(result.rows) - 200} more rows")
    console.print(f"Read: {escape(result.source)}")
    if result.errors:
        for e in result.errors:
            err.print(f"[red]{escape(e)}[/]")
        raise FinanceError(f"{len(result.errors)} problems; nothing was written.")
    if result.opening:
        when, value = result.opening
        verb = "would be added" if dry_run else "added"
        console.print(f"Opening balance {verb}: {money(value)} on {when} (the balance before the statement's first line)")
    learned = f", {result.learned} by the learned model" if result.learned else ""
    if result.loan_payments:
        learned += f", {result.loan_payments} loan installment{'s' if result.loan_payments != 1 else ''} split"
    if dry_run:
        console.print(
            f"[yellow]Dry run:[/] {result.new} new{learned}, {result.duplicates} already in, "
            f"{result.uncategorized} uncategorized; nothing was written."
        )
    else:
        hint = " (add rules or recategorize some, then run 'finance categorize')" if result.uncategorized else ""
        console.print(
            f"[green]{result.added} transactions added[/]{learned}, {result.duplicates} were already in, "
            f"{result.uncategorized} uncategorized{hint}."
        )
    for reference, lines in sorted(result.unknown_loans.items()):
        verb = "would go" if dry_run else "went"
        console.print(
            f"[yellow]Loan {escape(reference)}:[/] {lines} installment line{'s' if lines != 1 else ''} {verb} to a category, "
            "as no loan has this number. To split installments into principal, interest and taxes, add the loan first: "
            f"finance loan add NAME ACCOUNT … --reference {escape(reference)}"
        )
    check = result.balance_check
    if check is not None:
        ledger_text = "would be" if check.predicted else "is"
        if check.ok:
            console.print(f"[green]Balance check:[/] the bank's balance on {check.on} is {money(check.bank)}; the ledger's {ledger_text} the same.")
        else:
            err.print(
                f"[yellow]Balance check:[/] the bank's balance on {check.on} is {money(check.bank)}, the ledger's "
                f"{ledger_text} {money(check.ledger)} (difference {money(check.difference)}). Missing transactions, or an "
                "opening balance to enter (--opening for a new account)?"
            )


@app.command("import")
@guarded
def import_(
    ctx: typer.Context,
    file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="A CSV file, or a bank statement (.xlsx, .html)")],
    account: Annotated[
        Optional[str], typer.Option(help="The statement's account; for CSV, the account of rows without one")
    ] = None,
    bank: Annotated[str, typer.Option(help="Bank template: 'auto' or one of the templates (see README)")] = "auto",
    opening: Annotated[
        bool, typer.Option(help="Statements: start a new account at the balance before the first line")
    ] = False,
    delimiter: Annotated[str, typer.Option(help="CSV column delimiter: ';', ',', 'tab' or 'auto'")] = "auto",
    decimal: Annotated[str, typer.Option(help="CSV decimal separator: ',', '.' or 'auto'")] = "auto",
    date_format: Annotated[Optional[str], typer.Option(help="CSV, for example %d.%m.%Y (default: 2026-10-09 or 09.10.2026)")] = None,
    encoding: Annotated[Optional[str], typer.Option(help="CSV: utf-8, cp1254… (default: detected)")] = None,
    columns: Annotated[
        Optional[str], typer.Option(help="CSV mapping, e.g. 'date=Transaction Date,description=Details,amount=Amount'")
    ] = None,
    skip: Annotated[int, typer.Option(help="CSV: lines to skip before the header row")] = 0,
    invert_sign: Annotated[bool, typer.Option(help="CSV: spending is listed as positive amounts")] = False,
    learn: Annotated[bool, typer.Option(help="Let the learned model categorize what the rules do not")] = True,
    min_confidence: Annotated[float, typer.Option(help="Least probability for a learned category")] = 0.8,
    dry_run: Annotated[bool, typer.Option(help="Preview without writing anything")] = False,
) -> None:
    """Import a bank statement or a CSV file; rows already imported are skipped.

    Bank statements (.xlsx, .html) are recognized by their template, checked
    line by line against the bank's running balance and totals, and compared
    with the ledger afterwards. CSV format: README → Importing.
    """
    from financeproject.ingest.banks import (
        EXCEL_SUFFIXES,
        HTML_SUFFIXES,
        read_statement,
    )
    from financeproject.ingest.csvfile import CsvFormat, import_csv, parse_column_map
    from financeproject.ingest.importer import import_statement

    if not 0 < min_confidence <= 1:
        raise FinanceError("--min-confidence must be between 0 and 1.")
    if bank != "auto" or file.suffix.lower() in EXCEL_SUFFIXES + HTML_SUFFIXES:
        statement = read_statement(file, None if bank == "auto" else bank)
        if not account:
            raise FinanceError("A bank statement needs --account: the ledger account it belongs to.")
        period = f"{statement.start} – {statement.end}"
        balances = (
            f"; balance {money(statement.opening_balance)} → {money(statement.closing_balance)}"
            if statement.has_balances
            else ""
        )
        console.print(f"[bold]{escape(statement.bank)}[/] statement, {period}, {len(statement.lines)} lines{balances}")
        with ledger(ctx) as s:
            result = import_statement(
                s, statement, file, account, dry_run=dry_run, add_opening=opening, use_model=learn, min_confidence=min_confidence
            )
            _print_import(result, dry_run)
        return
    if opening:
        raise FinanceError("--opening works with bank statements, which carry balances; CSV files do not.")
    _check_choice(decimal, ("auto", ",", "."), "--decimal")
    fmt = CsvFormat(
        delimiter={"auto": None, "tab": "\t"}.get(delimiter, delimiter),
        decimal_sep=None if decimal == "auto" else decimal,
        date_format=date_format,
        encoding=encoding,
        columns=parse_column_map(columns) if columns else {},
        skip_rows=skip,
        invert_sign=invert_sign,
    )
    with ledger(ctx) as s:
        result = import_csv(s, file, fmt, account=account, dry_run=dry_run, use_model=learn, min_confidence=min_confidence)
        _print_import(result, dry_run)


@rule_app.command("add")
@guarded
def rule_add(
    ctx: typer.Context,
    text: Annotated[str, typer.Argument(help="Text to look for in the description (case and Turkish letters do not matter)")],
    account: Annotated[str, typer.Argument(help="Counter account of matching transactions")],
    regex: Annotated[bool, typer.Option(help="The text is a regular expression")] = False,
    priority: Annotated[int, typer.Option(help="Higher is tried first")] = 0,
) -> None:
    """Add a categorization rule."""
    from financeproject.ingest.rules import add_rule

    with ledger(ctx) as s:
        r = add_rule(s, text, account, regex, priority)
        console.print(f"[green]Rule #{r.id}:[/] '{escape(r.pattern)}' → {escape(r.account.path)}")


@rule_app.command("list")
@guarded
def rule_list(ctx: typer.Context) -> None:
    """Rules, in the order they are tried."""
    with ledger(ctx) as s:
        t = Table("#", "Text", "Regex", "Priority", "Account")
        for r in s.scalars(select(Rule).options(selectinload(Rule.account)).order_by(Rule.priority.desc(), Rule.id)):
            t.add_row(str(r.id), escape(r.pattern), "yes" if r.is_regex else "", str(r.priority), escape(r.account.path))
        console.print(t)


@rule_app.command("delete")
@guarded
def rule_delete(ctx: typer.Context, number: Annotated[int, typer.Argument(metavar="ID")]) -> None:
    """Delete a rule."""
    from financeproject.ingest.rules import delete_rule

    with ledger(ctx) as s:
        delete_rule(s, number)
    console.print(f"[yellow]Rule #{number} deleted.[/]")


@rule_app.command("apply")
@guarded
def rule_apply(ctx: typer.Context) -> None:
    """Apply the rules to uncategorized transactions."""
    from financeproject.ingest.rules import apply_rules_to_uncategorized

    with ledger(ctx) as s:
        moved = apply_rules_to_uncategorized(s)
        for tx, target in moved:
            console.print(f"#{tx.id} {tx.date} {escape(tx.description)} → {escape(target.path)}")
        console.print(f"[green]{len(moved)} transactions categorized.[/]")


# --------------------------------------------------------------------------- prices and CPI


@price_app.command("update")
@guarded
def price_update(
    ctx: typer.Context,
    start: Annotated[Optional[str], typer.Option(help="Default: the first transaction date (or 30 days ago)")] = None,
    end: Annotated[Optional[str], typer.Option(help="Default: today")] = None,
    only: Annotated[Optional[str], typer.Option(help="fx, gold or cpi")] = None,
) -> None:
    """Download TCMB exchange rates, gold (Frankfurter) and the CPI; fills in missing days."""
    from financeproject.prices.http import make_client
    from financeproject.prices.update import (
        UpdateReport,
        tcmb_currencies,
        update_cpi,
        update_fx,
        update_gold,
    )

    if only is not None:
        _check_choice(only, ("fx", "gold", "cpi"), "--only")
    today = date.today()
    key = config.evds_api_key()
    with ledger(ctx) as s:
        first, _ = transaction_date_range(s)
        start_day = parse_date(start) if start else (first or today - timedelta(days=30))
        end_day = parse_date(end) if end else today
        if end_day < start_day:
            raise FinanceError("The end date is before the start date.")
        report = UpdateReport()
        with make_client() as client, console.status("Downloading prices…") as progress:
            if only in (None, "fx"):
                report.merge(update_fx(s, client, start_day, end_day, tcmb_currencies(s), key, progress=progress.update))
                s.commit()
            if only in (None, "gold"):
                report.merge(update_gold(s, client, start_day, end_day, progress=progress.update))
                s.commit()
            if only in (None, "cpi"):
                progress.update("CPI")
                report.merge(update_cpi(s, client, key, today))
                s.commit()
    for line in report.messages:
        console.print(f"  {line}")
    for line in report.warnings[:20]:
        err.print(f"  [yellow]{escape(line)}[/]")
    console.print(f"[green]{report.added} new, {report.updated} updated records.[/]")


@price_app.command("add")
@guarded
def price_add(
    ctx: typer.Context,
    commodity: str,
    value: Annotated[str, typer.Argument(help="Price of one unit")],
    on: DateOption = None,
    quote: Annotated[str, typer.Option(help="Currency of the price")] = "TRY",
) -> None:
    """Enter a price by hand (fund, stock, house…). Automatic updates never overwrite it."""
    code, quote = commodity.strip().upper(), quote.strip().upper()
    with ledger(ctx) as s:
        for c in (code, quote):
            if s.get(Commodity, c) is None:
                raise FinanceError(f"Unknown commodity: {c}")
        price = amount(value)
        if price <= 0:
            raise FinanceError("The price must be positive.")
        result = upsert_price(s, code, quote, day(on), price, MANUAL)
    console.print(f"[green]{code}/{quote} {day(on)} = {money(price, 6)} ({result})[/]")


@price_app.command("list")
@guarded
def price_list(
    ctx: typer.Context,
    commodity: str,
    quote: Annotated[Optional[str], typer.Option(help="Default: TRY (USD for gold)")] = None,
    last: Annotated[int, typer.Option()] = 15,
) -> None:
    """The latest prices of a commodity."""
    code = commodity.strip().upper()
    quote_code = (quote or ("USD" if code == "XAU_G" else "TRY")).upper()
    with ledger(ctx) as s:
        rows = list(
            s.scalars(
                select(Price)
                .where(Price.commodity_code == code, Price.quote_code == quote_code)
                .order_by(Price.date.desc())
                .limit(last)
            )
        )
        t = table("Date", f"{code}/{quote_code}>", "Source")
        for p in reversed(rows):
            t.add_row(str(p.date), money(p.value, 6), p.source)
        console.print(t)
        if code == "XAU_G":
            r = load_pricebook(s).resolve("XAU_G", date.today())
            if r:
                console.print(f"Gram of gold today: {money(r.value, 2)} TRY (latest data {r.as_of})")


@app.command()
@guarded
def cpi(
    ctx: typer.Context,
    last: Annotated[int, typer.Option(help="Number of months")] = 13,
) -> None:
    """Turkish CPI (2025=100): level, monthly and annual change, source."""
    with ledger(ctx) as s:
        index = load_index(s)
        if not index:
            raise FinanceError("No CPI: run 'finance init' or 'finance price update --only cpi'.")
        mo, an = monthly_pct(index), annual_pct(index)
        t = table("Month", "Index (2025=100)>", "Monthly %>", "Annual %>")
        for m in sorted(index)[-last:]:
            t.add_row(month_label(m), money(index[m], 2), money(mo.get(m), 2), money(an.get(m), 2))
        console.print(t)
        console.print("Source: " + ", ".join(f"{k} ({v} months)" for k, v in index_sources(s).items()))


# --------------------------------------------------------------------------- reports


SOURCE_LABELS = {
    "tcmb": "TCMB forex buying",
    "evds": "EVDS forex buying",
    "frankfurter": "Frankfurter XAU/USD",
    MANUAL: "entered by hand",
    "synthetic": "made up (demo)",
}
CpiMethodOption = Annotated[str, typer.Option(help="interpolate or monthly")]


def _price_note(book: PriceBook, code: str, at: date) -> str:
    r = book.resolve(code, at)
    if r is None:
        return "no price"
    sources = " × ".join(dict.fromkeys(SOURCE_LABELS.get(q.source, q.source) for q in r.path))
    unit = "TRY/g" if code == "XAU_G" else "TRY"
    return f"{money(r.value, 4 if code == 'USD' else 2)} {unit} · {r.as_of} · {sources}"


def _cpi_method(value: str) -> str:
    return _check_choice(value, Deflator.METHODS, "--cpi-method")


@app.command("balance-sheet")
@guarded
def balance_sheet(
    ctx: typer.Context,
    on: DateOption = None,
    owner: Annotated[Optional[str], typer.Option(help="Only this person's accounts; 'shared' = shared accounts")] = None,
    cpi_method: CpiMethodOption = "interpolate",
) -> None:
    """Personal balance sheet: assets, debts, net worth in four units, liquidity."""
    from financeproject.reports.balance_sheet import build_balance_sheet
    from financeproject.reports.context import load_market_data

    at = day(on)
    with ledger(ctx) as s:
        market = load_market_data(s, _cpi_method(cpi_method))
        bs = build_balance_sheet(s, at, owner=owner, market=market)
        title = f"Balance sheet · {at}" + (f" · {escape(owner)}" if owner else "")
        if bs.empty:
            console.print(f"[bold]{title}[/]\nNo account has a balance yet. Start with 'finance account add' and 'finance opening'.")
            return
        console.print(f"[bold]{title}[/]")

        a = table("Asset", "Unit", "Quantity>", "Price (TRY)>", "Value (TRY)>", "Liquidity", "Owner")
        for v in bs.assets:
            p = v.position
            price = "—" if v.price is None else ("" if p.commodity == "TRY" else money(v.price.value, 4))
            if v.stale:
                price += f" [yellow](stale: {v.price.as_of})[/]"
            a.add_row(
                escape(p.account),
                p.commodity,
                money(p.quantity, _decimals(s, p.commodity)),
                price,
                "[red]no price[/]" if v.value is None else money(v.value),
                TIERS.get(p.tier, ""),
                escape(p.owner or ""),
            )
        a.add_section()
        a.add_row("[bold]Total assets[/]", "", "", "", f"[bold]{money(bs.net_worth.assets)}[/]", "", "")
        console.print(a)

        if bs.liabilities:
            b = table("Liability", "Unit", "Owed>", "Value (TRY)>", "Term", "Owner")
            for v in bs.liabilities:
                p = v.position
                b.add_row(
                    escape(p.account),
                    p.commodity,
                    money(-p.quantity, _decimals(s, p.commodity)),
                    "[red]no price[/]" if v.value is None else money(-v.value),
                    TERMS[debt_term(p.kind)],
                    escape(p.owner or ""),
                )
            b.add_section()
            b.add_row("[bold]Total liabilities[/]", "", "", f"[bold]{money(bs.net_worth.liabilities)}[/]", "", "")
            console.print(b)

        nw = bs.net_worth
        u = nw.net
        conv = nw.converter
        lines = [f"[bold]TRY            {money(u.try_):>20}[/]"]
        if u.real_try is not None and market.base_month:
            note = f"in {month_label(market.base_month)} prices"
            if at < market.deflator.first_month:
                note += f"; the CPI starts in {month_label(market.deflator.first_month)}, its first month is used before that"
            elif conv.cpi_extrapolated:
                note += "; no CPI after that yet"
            lines.append(f"Real TRY       {money(u.real_try):>20}   [dim]{note}[/]")
        else:
            lines.append("Real TRY       [dim]no CPI (finance price update --only cpi)[/]")
        lines.append(
            f"USD            {money(u.usd):>20}   [dim]{_price_note(market.book, 'USD', at)}[/]"
            if u.usd is not None
            else "USD            [dim]no USD rate (finance price update)[/]"
        )
        lines.append(
            f"Grams of gold  {money(u.gold_g):>18} g   [dim]{_price_note(market.book, 'XAU_G', at)}[/]"
            if u.gold_g is not None
            else "Grams of gold  [dim]no gold price (finance price update)[/]"
        )
        console.print(Panel("\n".join(lines), title="Net worth", title_align="left", expand=False))

        liq = bs.liquidity
        t = table("Liquidity", "TRY>")
        for tier, value in liq.by_tier.items():
            t.add_row(TIERS[tier], money(value))
        t.add_section()
        t.add_row("Liquid assets (≤ 1 week)", money(liq.liquid_assets))
        t.add_row("Short-term debt", money(liq.short_term_debt))
        t.add_row("[bold]Liquid net assets[/]", f"[bold]{money(liq.liquid_net)}[/]")
        console.print(t)

        if not owner and len(bs.by_owner) > 1:
            o = table("Owner", "Net (TRY)>")
            for owner_, value in sorted(bs.by_owner.items()):
                o.add_row(escape(owner_), money(value))
            console.print(o)

        for item in nw.missing:
            err.print(f"[red]No price, left out of net worth:[/] {escape(item)} → 'finance price add' or 'finance price update'")
        for item in nw.stale:
            err.print(f"[yellow]Price older than 7 days:[/] {escape(item)}")


@app.command()
@guarded
def networth(
    ctx: typer.Context,
    start: Annotated[Optional[str], typer.Option(help="Default: the first transaction date")] = None,
    end: Annotated[Optional[str], typer.Option(help="Default: today")] = None,
    frequency: Annotated[str, typer.Option(help="month, week or day")] = "month",
    owner: Annotated[Optional[str], typer.Option()] = None,
    csv_file: Annotated[Optional[Path], typer.Option("--csv", help="Also write the table as CSV")] = None,
    cpi_method: CpiMethodOption = "interpolate",
) -> None:
    """Net worth over time (TRY, real TRY, USD, grams of gold)."""
    from financeproject.reports.context import load_market_data
    from financeproject.reports.networth import networth_series

    _check_choice(frequency, FREQUENCIES, "--frequency")
    with ledger(ctx) as s:
        market = load_market_data(s, _cpi_method(cpi_method))
        frame = networth_series(
            s,
            parse_date(start) if start else None,
            parse_date(end) if end else None,
            frequency,
            owner,
            market,
        )
    if frame.empty:
        console.print("No transactions yet.")
        return
    base = f" (in {month_label(market.base_month)} prices)" if market.base_month else ""
    t = table("Date", "TRY>", f"Real TRY{base}>", "USD>", "Grams of gold>", "Assets>", "Liabilities>", "Liquid net>")
    for r in frame.itertuples(index=False):
        flag = " [red]*[/]" if r.missing_prices else ""
        t.add_row(
            f"{r.date}{flag}",
            f"[bold]{money(r.nominal_try)}[/]",
            money(r.real_try),
            money(r.usd),
            money(r.gold_g),
            money(r.assets),
            money(r.liabilities),
            money(r.liquid_net),
        )
    console.print(t)
    if frame["missing_prices"].any():
        err.print("[red]*[/] Some assets have no price; net worth is incomplete on those dates.")
    if csv_file:
        frame.to_csv(csv_file, index=False)
        console.print(f"CSV: {csv_file}")


@app.command()
@guarded
def chart(
    ctx: typer.Context,
    file: Annotated[Optional[Path], typer.Option(help="PNG path (default: networth.png in the output folder)")] = None,
    start: Annotated[Optional[str], typer.Option()] = None,
    end: Annotated[Optional[str], typer.Option()] = None,
    frequency: Annotated[str, typer.Option(help="month, week or day")] = "week",
    owner: Annotated[Optional[str], typer.Option()] = None,
) -> None:
    """Net worth chart (PNG): TRY on top, real TRY, USD and grams of gold below."""
    from financeproject.reports.chart import save_networth_chart
    from financeproject.reports.context import load_market_data
    from financeproject.reports.networth import networth_series

    _check_choice(frequency, FREQUENCIES, "--frequency")
    with ledger(ctx) as s:
        market = load_market_data(s)
        frame = networth_series(
            s, parse_date(start) if start else None, parse_date(end) if end else None, frequency, owner, market
        )
    if frame.empty:
        raise FinanceError("No transactions yet.")
    target = file or config.output_dir() / "networth.png"
    subtitle = f"{frame['date'].iloc[0]} – {frame['date'].iloc[-1]}" + (f" · {owner}" if owner else "")
    save_networth_chart(frame, target, market.base_month, subtitle)
    console.print(f"[green]Chart:[/] {target}")


# --------------------------------------------------------------------------- backup and demo


@app.command("backup")
@guarded
def backup_(
    ctx: typer.Context,
    target: Annotated[Optional[Path], typer.Option(help="Backup folder (default: backups in the data directory)")] = None,
) -> None:
    """Make a consistent copy of the ledger (SQLite backup API)."""
    path: Path = ctx.obj.db_path
    if not path.exists():
        raise FinanceError(f"No ledger at {path}")
    copy = backup(path, target or config.backup_dir())
    console.print(f"[green]Backup:[/] {copy}")


@app.command()
@guarded
def demo(
    ctx: typer.Context,
    start: Annotated[Optional[str], typer.Option(help="Default: 12 months ago")] = None,
    end: Annotated[Optional[str], typer.Option(help="Default: today")] = None,
    synthetic: Annotated[bool, typer.Option(help="Offline: made-up exchange rates and gold prices")] = False,
    overwrite: Annotated[bool, typer.Option(help="Delete an existing demo ledger and build it again")] = False,
) -> None:
    """Build a separate demo ledger with a made-up household (your own ledger is not touched)."""
    from financeproject.demo import SYNTHETIC_SOURCE, build_demo, synthetic_prices
    from financeproject.prices.http import make_client
    from financeproject.prices.update import load_bundled_cpi, update_fx, update_gold
    from financeproject.reports.context import load_market_data

    real_ledger = config.default_db_path().resolve()
    path: Path = ctx.obj.db_path if ctx.obj.explicit else config.demo_db_path()
    if path.resolve() == real_ledger:
        raise FinanceError("Demo data never goes into your real ledger; give another --db.")
    if path.exists():
        if not overwrite:
            raise FinanceError(f"{path} already exists. Use --overwrite to build it again.")
        path.unlink()
    end_day = day(end)
    start_day = parse_date(start) if start else add_months(month_start(end_day), -12)
    engine = make_engine(path)
    try:
        init_db(engine)
        with session_scope(engine) as s:
            load_bundled_cpi(s)
            source = "made up"
            if not synthetic:
                try:
                    with make_client() as client, console.status("Downloading real exchange rates and gold prices…") as st:
                        update_fx(
                            s, client, start_day - timedelta(days=7), end_day, ["USD"], config.evds_api_key(),
                            pause=0.1, progress=st.update,
                        )
                        update_gold(s, client, start_day - timedelta(days=7), end_day, progress=st.update)
                    source = "TCMB + Frankfurter"
                except (httpx.HTTPError, FinanceError) as exc:
                    err.print(f"[yellow]Download failed ({escape(str(exc))}); using made-up prices.[/]")
                    s.rollback()
                    load_bundled_cpi(s)
                    synthetic = True
            if synthetic:
                for q in synthetic_prices(start_day - timedelta(days=7), end_day):
                    upsert_price(s, q.commodity, q.quote, q.on, q.value, SYNTHETIC_SOURCE)
            s.flush()
            market = load_market_data(s)
            summary = build_demo(s, start_day, end_day, market.book, market.deflator)
    finally:
        engine.dispose()
    path.chmod(0o600)
    console.print(
        f"[green]Demo ledger:[/] {path}\n  {summary.start} – {summary.end}, {summary.transactions} transactions, "
        f"prices: {source}\nTo look at it:\n  finance -d \"{path}\" balance-sheet\n  finance -d \"{path}\" networth\n"
        f"  finance -d \"{path}\" chart"
    )


# --------------------------------------------------------------------------- 01: categories, installments, flows, ratios


def _pct(value: float | None, decimals: int = 1) -> str:
    return "—" if value is None else f"{value * 100:.{decimals}f} %"


def _month_range(start: str | None, end: str | None, months: int, today: date) -> tuple[date, date]:
    from financeproject.core.dates import parse_month

    if months < 1:
        raise FinanceError("--months must be at least 1.")
    last = parse_month(end) if end else month_start(today)
    first = parse_month(start) if start else add_months(last, -(months - 1))
    if first > last:
        raise FinanceError("--start is after --end.")
    return first, last


def _short_month(m: date) -> str:
    from financeproject.core.dates import MONTHS_SHORT

    return f"{MONTHS_SHORT[m.month - 1]} {m:%y}"


@app.command()
@guarded
def categorize(
    ctx: typer.Context,
    min_confidence: Annotated[float, typer.Option(help="Least probability for a learned category")] = 0.8,
    dry_run: Annotated[bool, typer.Option(help="Show what would change without changing it")] = False,
    evaluate: Annotated[
        bool, typer.Option(help="Measure how well the model would have guessed the ledger so far")
    ] = False,
) -> None:
    """Categorize uncategorized transactions: the rules first, then the model learned from the ledger."""
    from financeproject.ingest import learn
    from financeproject.ingest.rules import rule_matches
    from financeproject.ledger.db import UNCATEGORIZED_EXPENSE, UNCATEGORIZED_INCOME

    if not 0 < min_confidence <= 1:
        raise FinanceError("--min-confidence must be between 0 and 1.")
    with ledger(ctx) as s:
        if evaluate:
            items = learn.examples(s)
            ev = learn.evaluate(items, min_confidence)
            t = table("Measure", "Value>")
            t.add_row("Categorized simple transactions", str(len(items)))
            t.add_row(f"Guessed in date order (after the first {learn.MIN_EXAMPLES})", str(ev.examples))
            t.add_row("Right, among all guesses", _pct(ev.accuracy))
            t.add_row(f"Guesses with probability ≥ {min_confidence:g}", _pct(ev.coverage))
            t.add_row("Right, among those (what would be applied)", _pct(ev.precision))
            console.print(t)
            console.print("[dim]Each transaction was guessed from the earlier ones only, then learned (prequential evaluation).[/]")
            return
        matches = rule_matches(s)
        matched = {tx.id for tx, _, _ in matches}
        model = learn.train(s)
        suggestions = [x for x in (learn.suggest(s, model, min_confidence) if model else []) if x.transaction.id not in matched]
        if matches or suggestions:
            t = table("#>", "Date", "Description", "Amount>", "New category", "How")
            for tx, posting, rule in matches:
                t.add_row(
                    str(tx.id), str(tx.date), escape((tx.payee or tx.description)[:40]), money(posting.amount),
                    escape(rule.account.path), f"rule #{rule.rule_id}",
                )
            for x in suggestions:
                tx = x.transaction
                t.add_row(
                    str(tx.id), str(tx.date), escape((tx.payee or tx.description)[:40]), money(x.posting.amount),
                    escape(x.account.path), f"model {x.confidence:.2f}",
                )
            console.print(t)
        if not dry_run:
            for _, posting, rule in matches:
                posting.account = rule.account
            learn.apply(s, suggestions)
        left = s.scalar(
            select(func.count())
            .select_from(Posting)
            .join(Account, Posting.account_id == Account.id)
            .where(Account.path.in_((UNCATEGORIZED_EXPENSE, UNCATEGORIZED_INCOME)))
        )
        if model is None:
            console.print(f"[dim]The model needs at least {learn.MIN_EXAMPLES} categorized transactions to learn from.[/]")
        changed = len(matches) + len(suggestions)
        if dry_run:
            console.print(f"[yellow]Dry run:[/] {changed} would be categorized ({len(matches)} by rules, {len(suggestions)} by the model).")
        else:
            console.print(
                f"[green]{changed} categorized[/] ({len(matches)} by rules, {len(suggestions)} by the model); "
                f"{left} still uncategorized."
            )


@app.command("installments")
@guarded
def installments_cmd(
    ctx: typer.Context,
    months: Annotated[int, typer.Option(help="How many months of the schedule to show")] = 12,
    on: DateOption = None,
) -> None:
    """Installment purchases, what falls due in the coming months, and each card's balance split."""
    from collections import defaultdict

    from financeproject.ledger.installments import not_billed, plans, schedule

    at = day(on)
    current = month_start(at)
    with ledger(ctx) as s:
        items = [p for p in plans(s) if p.transaction.date <= at]
        if not items:
            console.print(
                "No installment purchases yet. Record one with: "
                "finance expense AMOUNT CATEGORY --from CARD --installments N"
            )
            return
        t = table("Bought", "Description", "Card", "Total>", "Billed>", "Billed amount>", "Still to come>", "Last month")
        dues: dict[date, Decimal] = defaultdict(Decimal)
        for plan in items:
            parts = schedule(plan)
            billed = [i for i in parts if i.month <= current]
            done = sum((i.amount for i in billed), Decimal(0))
            for i in parts:
                if current <= i.month < add_months(current, months):
                    dues[i.month] += i.amount
            t.add_row(
                str(plan.transaction.date), escape(plan.transaction.description[:40]), escape(plan.account.path),
                money(plan.total), f"{len(billed)}/{plan.count}", money(done), money(plan.total - done),
                f"{parts[-1].month:%Y-%m}",
            )
        console.print(t)
        if dues:
            d = table("Month", "Installments due>")
            for m in sorted(dues):
                d.add_row(f"{m:%Y-%m}" + (" (this month)" if m == current else ""), money(dues[m]))
            console.print(d)
        later = not_billed(s, current, at)
        if later:
            c = table("Card", "Balance owed>", "Installments of later months>", "Rest>")
            for path, value in sorted(later.items()):
                acc = acc_mod.find_account(s, path)
                owed = -account_balance(s, acc, at).get(acc.commodity_code or "TRY", Decimal(0))
                c.add_row(escape(path), money(owed), money(value), money(owed - value))
            console.print(c)
            console.print("[dim]Rest = what the card owes apart from installments not billed yet.[/]")


@app.command("income-statement")
@guarded
def income_statement_cmd(
    ctx: typer.Context,
    start: Annotated[Optional[str], typer.Option(help="First month, e.g. 2026-04")] = None,
    end: Annotated[Optional[str], typer.Option(help="Last month (default: this month)")] = None,
    months: Annotated[int, typer.Option(help="Number of months when --start is not given")] = 6,
    depth: Annotated[int, typer.Option(help="Account levels to show, e.g. 2 = Expenses:Food")] = 2,
    real: Annotated[bool, typer.Option(help="In real TRY (prices of the latest CPI month)")] = False,
    csv_file: Annotated[Optional[Path], typer.Option("--csv", help="Also write the table as CSV")] = None,
) -> None:
    """Income and expenses by category and month, net saving and savings rate."""
    import pandas as pd

    from financeproject.core.dates import month_end
    from financeproject.reports.context import load_market_data
    from financeproject.reports.income import IncomeStatement, income_statement

    today = date.today()
    first, last = _month_range(start, end, months, today)
    with ledger(ctx) as s:
        market = load_market_data(s)
        st = income_statement(s, first, last, depth, "real" if real else "try", market, end=today)
    partial = st.months and st.end < month_end(st.months[-1])
    heads = [_short_month(m) + ("*" if partial and m == st.months[-1] else "") for m in st.months]
    t = table("", *(h + ">" for h in heads), "Total>", "Per month>")
    n = len(st.months)
    records = []

    def add(label: str, by_month: dict[date, Decimal], bold: bool = False, section: str = "") -> None:
        total = sum(by_month.values(), Decimal(0))
        cells = [money(by_month.get(m), 0) if by_month.get(m) else "" for m in st.months]
        row = [escape(label), *cells, money(total, 0), money(total / n, 0)]
        t.add_row(*([f"[bold]{c}[/]" for c in row] if bold else row))
        records.append({"section": section, "row": label, **{f"{m:%Y-%m}": float(by_month.get(m, 0)) for m in st.months}, "total": float(total)})

    def totals(table_: dict[str, dict[date, Decimal]]) -> dict[date, Decimal]:
        out: dict[date, Decimal] = {}
        for by_month in table_.values():
            for m, v in by_month.items():
                out[m] = out.get(m, Decimal(0)) + v
        return out

    t.add_row("[bold]Income[/]", *[""] * (n + 2))
    for key in IncomeStatement.totals(st.income):
        add(f"  {key}", st.income[key], section="income")
    add("Total income", totals(st.income), True, "income")
    t.add_section()
    t.add_row("[bold]Expenses[/]", *[""] * (n + 2))
    for key in IncomeStatement.totals(st.expenses):
        add(f"  {key}", st.expenses[key], section="expenses")
    add("Total expenses", totals(st.expenses), True, "expenses")
    t.add_section()
    net = {m: st.net(m) for m in st.months}
    add("Net saving", net, True, "net")
    rates = [_pct(st.savings_rate(m), 0) for m in st.months]
    t.add_row("Savings rate", *rates, _pct(st.savings_rate(), 0), "")
    unit = f"real TRY, in {month_label(market.base_month)} prices" if real else "TRY"
    console.print(f"[bold]Income statement[/] · {st.start} – {st.end} · {unit}")
    console.print(t)
    if partial:
        console.print(f"[dim]* {month_label(st.months[-1])} so far (to {st.end}).[/]")
    for item in st.unvalued[:10]:
        err.print(f"[yellow]No price, left out:[/] {escape(item)}")
    if csv_file:
        pd.DataFrame(records).to_csv(csv_file, index=False)
        console.print(f"CSV: {csv_file}")


@app.command("cash-flow")
@guarded
def cash_flow_cmd(
    ctx: typer.Context,
    start: Annotated[Optional[str], typer.Option(help="First month, e.g. 2026-04")] = None,
    end: Annotated[Optional[str], typer.Option(help="Last month (default: this month)")] = None,
    months: Annotated[int, typer.Option(help="Number of months when --start is not given")] = 6,
    real: Annotated[bool, typer.Option(help="In real TRY (prices of the latest CPI month)")] = False,
) -> None:
    """Where the money in the cash accounts (tier 0) came from and went, month by month."""
    from financeproject.reports.cashflow import LINES, cash_flow
    from financeproject.reports.context import load_market_data

    today = date.today()
    first, last = _month_range(start, end, months, today)
    with ledger(ctx) as s:
        market = load_market_data(s)
        flows = cash_flow(s, first, last, "real" if real else "try", market, end=today)
    if not flows:
        console.print("No months to show.")
        return
    t = table("", *(_short_month(f.month) + ">" for f in flows), "Total>")

    def add(label: str, values: list[Decimal], bold: bool = False, total: Decimal | None = None) -> None:
        cells = [escape(label), *(money(v, 0) for v in values), "" if total is None else money(total, 0)]
        t.add_row(*([f"[bold]{c}[/]" for c in cells] if bold else cells))

    add("Cash at the start", [f.beginning for f in flows], True)
    t.add_section()
    for key, label, _ in LINES:
        values = [f.line(key) for f in flows]
        if key in ("income", "expenses", "operating", "investing", "cards", "loans") or any(abs(v) >= Decimal("0.5") for v in values):
            add(("" if key == "operating" else "  ") + label, values, key == "operating", sum(values, Decimal(0)))
    values = [f.revaluation for f in flows]
    add("  Revaluation of foreign currency", values, total=sum(values, Decimal(0)))
    t.add_section()
    add("Cash at the end", [f.ending for f in flows], True)
    unit = f"real TRY, in {month_label(market.base_month)} prices" if real else "TRY"
    console.print(f"[bold]Cash flow[/] · cash, checking and foreign currency accounts · {unit}")
    console.print(t)
    console.print(
        "[dim]Card purchases leave cash when the card is paid (Card payments), not when they are made. "
        "Foreign currency held as cash is revalued at each day's exchange rate.[/]"
    )
    for f in flows:
        if f.decomposition.unvalued:
            err.print(f"[yellow]{f.month:%Y-%m}: left out (no price):[/] {', '.join(f.decomposition.unvalued)}")


@app.command()
@guarded
def sankey(
    ctx: typer.Context,
    month: Annotated[Optional[str], typer.Option(help="One month, e.g. 2026-09 (default: last month)")] = None,
    start: Annotated[Optional[str], typer.Option(help="First month of a longer period")] = None,
    end: Annotated[Optional[str], typer.Option(help="Last month of a longer period")] = None,
    depth: Annotated[int, typer.Option(help="Account levels, e.g. 2 = Expenses:Food")] = 2,
    file: Annotated[Optional[Path], typer.Option(help="PNG path (default: in the output folder)")] = None,
) -> None:
    """Sankey diagram (PNG): income sources → budget → expense categories and savings."""
    from financeproject.core.dates import parse_month
    from financeproject.reports.context import load_market_data
    from financeproject.reports.income import IncomeStatement, income_statement
    from financeproject.reports.sankey import build, save_sankey

    today = date.today()
    if month:
        first = last = parse_month(month)
    elif start or end:
        first, last = _month_range(start, end, 1, today)
    else:
        first = last = add_months(month_start(today), -1)
    with ledger(ctx) as s:
        st = income_statement(s, first, last, depth, "try", load_market_data(s), end=today)
    data = build(IncomeStatement.totals(st.income), IncomeStatement.totals(st.expenses))
    period = month_label(first) if first == last else f"{_short_month(first)} – {_short_month(last)}"
    target = file or config.output_dir() / (
        f"sankey-{first:%Y-%m}.png" if first == last else f"sankey-{first:%Y-%m}-{last:%Y-%m}.png"
    )
    save_sankey(
        data, target, f"Where the money went · {period}",
        footnote=f"{st.start} – {st.end}. TRY on the day of each transaction"
        + ("; the smallest categories are grouped as Other." if data.grouped else "."),
    )
    console.print(f"[green]Sankey:[/] {target}")


@app.command()
@guarded
def ratios(
    ctx: typer.Context,
    on: DateOption = None,
    months: Annotated[int, typer.Option(help="Months of income and expenses to average over")] = 12,
) -> None:
    """Household financial ratios against the benchmarks of Greninger et al. (1996)."""
    from financeproject.reports.context import load_market_data
    from financeproject.reports.ratios import BELOW, DANGER, OK, household_ratios

    if months < 1:
        raise FinanceError("--months must be at least 1.")
    at = day(on)
    with ledger(ctx) as s:
        report = household_ratios(s, at, months, load_market_data(s))
    labels = {OK: "[green]✓ ok[/]", BELOW: "[yellow]• below[/]", DANGER: "[red]▲ danger[/]", None: ""}
    t = table("Ratio", "Value>", "Benchmark", "Status", "What it is")
    for r in report.ratios:
        if r.value is None:
            value = "—"
        elif r.unit == "months":
            value = f"{r.value:.1f} months"
        elif r.unit == "%":
            value = f"{r.value:.1f} %"
        else:
            value = f"{r.value:.2f} ×"
        t.add_row(r.name, value, r.benchmark, labels[r.status], escape(r.note))
    console.print(f"[bold]Household ratios[/] · flows {report.start + timedelta(days=1)} – {report.end} ({months} months) · balances on {report.end}")
    console.print(t)
    inputs = table("Input", "TRY>")
    for label, value in (
        ("Income", report.income),
        ("Expenses", report.expenses),
        ("Loan principal repaid", report.principal_repaid),
        ("Monthly expenses (incl. principal)", report.monthly_expenses),
        ("Loan payments (non-mortgage)", report.loan_payments),
        ("Mortgage payments", report.mortgage_payments),
        ("Card installments due", report.installments_due),
        ("Liquid assets", report.liquid),
        ("Investment assets", report.investments),
        ("Net worth", report.net_worth),
    ):
        inputs.add_row(label, money(value))
    console.print(inputs)
    console.print(
        "[dim]Benchmarks: Greninger et al. (1996), a Delphi study among US financial planners and educators. "
        "They are signals for a closer look, not advice.[/]"
    )


@app.command()
@guarded
def change(
    ctx: typer.Context,
    month: Annotated[Optional[str], typer.Option(help="One month, e.g. 2026-09 (default: this month so far)")] = None,
    start: Annotated[Optional[str], typer.Option(help="From the end of this day…")] = None,
    end: Annotated[Optional[str], typer.Option(help="…to the end of this day (default: today)")] = None,
    unit: Annotated[str, typer.Option(help="try, real, usd or gold")] = "try",
    top: Annotated[int, typer.Option(help="Accounts to show under income and expenses")] = 5,
) -> None:
    """Why net worth changed: income, expenses, exchange results and revaluation."""
    from financeproject.core.dates import month_end, parse_month
    from financeproject.reports.change import UNITS, networth_change
    from financeproject.reports.context import load_market_data

    _check_choice(unit, tuple(UNITS), "--unit")
    today = date.today()
    if month:
        m = parse_month(month)
        a, b = month_start(m) - timedelta(days=1), min(month_end(m), today)
    else:
        a = parse_date(start) if start else month_start(today) - timedelta(days=1)
        b = parse_date(end) if end else today
    with ledger(ctx) as s:
        market = load_market_data(s)
        d = networth_change(s, a, b, unit, market)
    decimals = 2 if unit in ("usd", "gold") else 0
    suffix = {"try": "TRY", "real": "TRY", "usd": "USD", "gold": "g"}[unit]

    def fmt(v: Decimal, sign: bool = True) -> str:
        return f"{format_amount(v, decimals, sign=sign)} {suffix}"

    t = Table(show_header=False, box=None, pad_edge=False)
    t.add_column("")
    t.add_column("", justify="right")
    t.add_row(f"[bold]Net worth on {a}[/]", f"[bold]{fmt(d.start_value, False)}[/]")
    for key, label in (("income", "Income"), ("expenses", "Expenses")):
        t.add_row(label, fmt(d.flows.get(key, Decimal(0))))
        detail = sorted(d.details.get(key, {}).items(), key=lambda kv: -abs(kv[1]))
        for path, v in detail[:top]:
            t.add_row(f"  [dim]{escape(path)}[/]", f"[dim]{fmt(v)}[/]")
        if len(detail) > top:
            rest = sum((v for _, v in detail[top:]), Decimal(0))
            t.add_row(f"  [dim]{len(detail) - top} more[/]", f"[dim]{fmt(rest)}[/]")
    saving = d.flows.get("income", Decimal(0)) + d.flows.get("expenses", Decimal(0))
    t.add_row("[bold]Net saving[/]", f"[bold]{fmt(saving)}[/]")
    shown = Decimal(1).scaleb(-decimals) / 2
    for key, label in (
        ("exchange", "Exchange results (buying and selling currency or gold)"),
        ("opening", "Opening balances (accounts added to the ledger)"),
        ("equity", "Other equity"),
        ("unvalued", "Moved to or from assets without a price"),
    ):
        if abs(d.flows.get(key, Decimal(0))) >= shown:
            t.add_row(label, fmt(d.flows[key]))
    if d.revaluation:
        t.add_row("Revaluation", fmt(d.revaluation_total))
        for commodity, v in sorted(d.revaluation.items(), key=lambda kv: -abs(kv[1])):
            name = commodity
            if commodity == "TRY" and unit == "real":
                name = "TRY (inflation)"
            t.add_row(f"  [dim]{name}[/]", f"[dim]{fmt(v)}[/]")
    t.add_row(f"[bold]Net worth on {b}[/]", f"[bold]{fmt(d.end_value, False)}[/]")
    title = {"try": "TRY", "real": f"real TRY, in {month_label(market.base_month) if market.base_month else '?'} prices",
             "usd": "US dollars", "gold": "grams of gold"}[unit]
    console.print(f"[bold]Change in net worth[/] · {title}")
    console.print(t)
    if abs(d.residual) > Decimal("0.01"):
        err.print(f"[red]The parts do not add up (difference {d.residual}); please report this.[/]")
    if d.unvalued:
        err.print(f"[yellow]Left out, no price on some days:[/] {', '.join(d.unvalued)}")


@app.command("recurring")
@guarded
def recurring_cmd(
    ctx: typer.Context,
    all_: Annotated[bool, typer.Option("--all", help="Also show series that seem to have stopped")] = False,
    on: Annotated[Optional[str], typer.Option("--date", help="Judge the status as of this day (default: today)")] = None,
) -> None:
    """Recurring payments and income found in the ledger: subscriptions, bills, rent, salary."""
    from financeproject.core.recurring import ACTIVE, LATE, STOPPED
    from financeproject.reports.recurring import recurring

    today = day(on)
    with ledger(ctx) as s:
        series = recurring(s)
    shown = [x for x in series if all_ or x.status(today) != STOPPED]
    if not shown:
        console.print("No recurring payments found yet (a monthly series needs at least three payments).")
        return
    labels = {ACTIVE: "active", LATE: "[yellow]late[/]", STOPPED: "[dim]stopped?[/]"}
    t = table("", "Payee", "Category", "Every", "Amount>", "Times>", "Last", "Next", "Status")
    for x in shown:
        t.add_row(
            "in" if x.direction > 0 else "out",
            escape(x.label[:32]),
            escape(x.category),
            x.period.name,
            ("" if x.fixed else "≈ ") + f"{money(x.typical)} {x.commodity}",
            str(len(x.occurrences)),
            str(x.last.on),
            str(x.next_due),
            labels[x.status(today)],
        )
    console.print(t)
    monthly: dict[str, Decimal] = {}
    for x in shown:
        if x.direction < 0 and x.status(today) != STOPPED:
            monthly[x.commodity] = monthly.get(x.commodity, Decimal(0)) + x.typical * Decimal(30.44 / x.period.days)
    if monthly:
        total = " + ".join(f"{money(v, 0)} {c}" for c, v in sorted(monthly.items()))
        console.print(f"Recurring payments come to about {total} a month. [dim]≈ = the amount varies.[/]")


@app.command()
@guarded
def upcoming(
    ctx: typer.Context,
    days: Annotated[int, typer.Option(help="How many days ahead")] = 30,
    on: Annotated[Optional[str], typer.Option("--date", help="Look ahead from this day (default: today)")] = None,
) -> None:
    """Payments expected in the coming days: recurring payments, card and loan installments."""
    from financeproject.reports.recurring import upcoming as expected

    today = day(on)
    with ledger(ctx) as s:
        items = expected(s, today, days)
    if not items:
        console.print("Nothing expected (no recurring payments, installments or loans found).")
        return
    t = table("Date", "What", "In>", "Out>", "Unit", "Note")
    totals: dict[tuple[str, int], Decimal] = {}
    for u in items:
        when = f"{u.on:%Y-%m}" if u.kind == "installments" else str(u.on)
        if u.note == "overdue":
            when = f"[yellow]{when}[/]"
        totals[(u.commodity, u.direction)] = totals.get((u.commodity, u.direction), Decimal(0)) + u.amount
        t.add_row(
            when, escape(u.label[:40]), money(u.amount) if u.direction > 0 else "", money(u.amount) if u.direction < 0 else "",
            u.commodity, escape(u.note),
        )
    t.add_section()
    for commodity in sorted({c for c, _ in totals}):
        t.add_row(
            "[bold]Total[/]", "", f"[bold]{money(totals.get((commodity, 1), Decimal(0)))}[/]",
            f"[bold]{money(totals.get((commodity, -1), Decimal(0)))}[/]", commodity, "",
        )
    console.print(f"[bold]Expected[/] · {today} – {today + timedelta(days=days)}")
    console.print(t)


# --------------------------------------------------------------------------- 02: loans and debts


def _percent(text: str, option: str) -> Decimal:
    """'3.49' or '3,49' (per cent) → 0.0349."""
    value = parse_decimal(text)
    if value < 0:
        raise FinanceError(f"{option} cannot be negative.")
    return value / 100


def _rate(value: Decimal | float | None, decimals: int = 2) -> str:
    return "—" if value is None else f"{float(value) * 100:.{decimals}f} %"


def _assignments(items: list[str] | None, option: str, parse) -> dict[str, Decimal]:
    """['Borrowed=1000', …] → {'Borrowed': Decimal('1000')}."""
    out: dict[str, Decimal] = {}
    for item in items or []:
        name, sep, value = item.rpartition("=")
        if not sep or not name.strip() or not value.strip():
            raise FinanceError(f"{option} takes ACCOUNT=VALUE, for example {option} 'Borrowed=1000'.")
        out[name.strip()] = parse(value)
    return out


def _print_loan(loan, inferred: bool = False) -> None:
    from financeproject.ledger.loans import cost_rate, plan, terms_of

    items = plan(loan)
    terms = terms_of(loan)
    how = "worked out from the installment" if inferred else "contractual"
    taxes = f"; with KKDF {_rate(loan.kkdf, 0)} and BSMV {_rate(loan.bsmv, 0)} of the interest, {_rate(terms.gross_rate, 3)}"
    console.print(
        f"[green]{escape(loan.name)}[/] ({escape(loan.account.path)}): {money(loan.principal)} at "
        f"{_rate(loan.monthly_rate, 3)} a month ({how}){taxes if loan.kkdf or loan.bsmv else ''}"
    )
    if loan.method == "annuity":
        console.print(
            f"{loan.count} installments of {money(items[0].payment)} from {loan.first_due} to {items[-1].due} "
            f"(the last {money(items[-1].payment)})"
        )
    else:
        console.print(
            f"{loan.count} installments from {money(items[0].payment)} down to {money(items[-1].payment)}, "
            f"{loan.first_due} to {items[-1].due}"
        )
    total = sum((i.payment for i in items), Decimal(0))
    console.print(
        f"Paid back in all: {money(total + loan.fees)} = principal {money(loan.principal)} + interest "
        f"{money(sum((i.interest for i in items), Decimal(0)))} + taxes {money(sum((i.taxes for i in items), Decimal(0)))}"
        + (f" + fees {money(loan.fees)}" if loan.fees else "")
    )
    console.print(f"Annual cost rate (yıllık maliyet oranı): [bold]{_rate(cost_rate(loan))}[/]")
    if loan.start_number > 1:
        console.print(f"Installments 1–{loan.start_number - 1} count as paid before the ledger.")


@loan_app.command("add")
@guarded
def loan_add(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="A short name, e.g. 'Car loan'")],
    account: Annotated[str, typer.Argument(help="Its liability account, e.g. 'Liabilities:Loans:Car' (created if new)")],
    principal: Annotated[str, typer.Option(help="The amount borrowed")],
    count: Annotated[int, typer.Option(help="Number of monthly installments")],
    first_due: Annotated[str, typer.Option(help="Date of the first installment; the others fall on the same day of each month")],
    rate: Annotated[Optional[str], typer.Option(help="Contractual interest rate a month, per cent (e.g. 3.49)")] = None,
    installment: Annotated[
        Optional[str], typer.Option(help="Instead of --rate: the installment on your papers; the rate is worked out from it")
    ] = None,
    method: Annotated[str, typer.Option(help="annuity (equal installments) or equal-principal")] = "annuity",
    kkdf: Annotated[
        Optional[str], typer.Option(help="KKDF, per cent of the interest (default: 15 for consumer and vehicle loans, else 0)")
    ] = None,
    bsmv: Annotated[
        Optional[str], typer.Option(help="BSMV, per cent of the interest (default: 15 for consumer and vehicle loans, else 0)")
    ] = None,
    fees: Annotated[str, typer.Option(help="Paid when the loan was taken (file fee, insurance); part of the annual cost rate")] = "0",
    disbursed: Annotated[Optional[str], typer.Option(help="Day the loan was paid out (default: a month before the first installment)")] = None,
    reference: Annotated[
        Optional[str],
        typer.Option(help="The bank's number for the loan as statements show it: their installment lines are then split"),
    ] = None,
    paid_into: Annotated[Optional[str], typer.Option(help="A new loan: book the payout into this account")] = None,
    opening_on: Annotated[
        Optional[str], typer.Option(help="A loan from before the ledger: installments due by this day count as paid")
    ] = None,
    owed: Annotated[Optional[str], typer.Option(help="With --opening-on: the principal owed that day (default: from the plan)")] = None,
    kind: Annotated[str, typer.Option(help="Kind of a new account: consumer_loan, vehicle_loan, mortgage, other_debt, personal_debt")] = "consumer_loan",
    interest_account: Annotated[str, typer.Option(help="Expense account for the interest")] = "Expenses:Interest and Fees",
    tax_account: Annotated[Optional[str], typer.Option(help="Expense account for KKDF and BSMV (default: with the interest)")] = None,
) -> None:
    """Record a loan's terms; shows its payment plan and annual cost rate.

    The balance comes from --paid-into (a new loan), --opening-on (a loan taken
    before the ledger) or the account's own transactions. Rates are as on the
    contract: the monthly interest rate without KKDF and BSMV.
    """
    from financeproject.core.loans import ANNUITY, EQUAL_PRINCIPAL, implied_monthly_rate
    from financeproject.errors import AccountNotFound
    from financeproject.ledger.loans import DEFAULT_TAXES, LOAN_KINDS, add_loan

    methods = {"annuity": ANNUITY, "equal-principal": EQUAL_PRINCIPAL}
    _check_choice(method, tuple(methods), "--method")
    _check_choice(kind, LOAN_KINDS, "--kind")
    if (rate is None) == (installment is None):
        raise FinanceError("Give either the monthly interest rate (--rate) or the installment (--installment).")
    if owed is not None and opening_on is None:
        raise FinanceError("--owed needs --opening-on.")
    if count < 1:
        raise FinanceError("--count must be at least 1.")
    with ledger(ctx) as s:
        try:
            acc = acc_mod.find_account(s, account)
        except AccountNotFound:
            if not account.strip().casefold().startswith("liabilities:"):
                raise FinanceError(
                    f"No account matches {account!r}. To create one give its full path, e.g. 'Liabilities:Loans:{name}'."
                ) from None
            acc = acc_mod.create_account(s, account, kind=kind, commodity="TRY")
            console.print(f"Account created: {escape(acc.path)} ({kind})")
        default_kkdf, default_bsmv = DEFAULT_TAXES.get(acc.kind, (Decimal(0), Decimal(0)))
        k = _percent(kkdf, "--kkdf") if kkdf is not None else default_kkdf
        b = _percent(bsmv, "--bsmv") if bsmv is not None else default_bsmv
        p = amount(principal)
        if rate is not None:
            monthly = _percent(rate, "--rate")
        else:
            if method != "annuity":
                raise FinanceError("--installment works with equal installments (annuity); give --rate for equal principal.")
            try:
                monthly = implied_monthly_rate(p, amount(installment), count, 1 + k + b)
            except ValueError as exc:
                raise FinanceError(str(exc)) from exc
        loan = add_loan(
            s,
            name,
            acc,
            principal=p,
            monthly_rate=monthly,
            count=count,
            first_due=parse_date(first_due),
            disbursed=parse_date(disbursed) if disbursed else None,
            method=methods[method],
            kkdf=k,
            bsmv=b,
            fees=amount(fees),
            reference=reference,
            interest_account=interest_account,
            tax_account=tax_account,
            disburse_to=paid_into,
            opening_on=parse_date(opening_on) if opening_on else None,
            opening_owed=amount(owed) if owed is not None else None,
        )
        _print_loan(loan, inferred=installment is not None)


@loan_app.command("list")
@guarded
def loan_list(ctx: typer.Context, on: DateOption = None) -> None:
    """The loans: what they owe, what they cost, what is left to pay."""
    from financeproject.ledger.loans import all_loans, cost_rate, loan_state

    at = day(on)
    with ledger(ctx) as s:
        loans = all_loans(s)
        if not loans:
            console.print(
                "No loans yet. Add one with: finance loan add NAME ACCOUNT --principal … --rate … --count … --first-due …"
            )
            return
        t = table("Loan", "Account", "Owed>", "Rate a month>", "Cost a year>", "Paid>", "Next due", "Next>", "Left to pay>", "Last")
        replanned = False
        for loan in loans:
            st = loan_state(s, loan, at)
            nxt = st.next
            replanned |= st.replanned
            due = "—" if nxt is None else (f"[yellow]{nxt.due} overdue[/]" if nxt.due < at else str(nxt.due))
            t.add_row(
                escape(loan.name), escape(loan.account.path), money(st.owed), _rate(loan.monthly_rate, 3), _rate(cost_rate(loan)),
                f"{st.paid}/{loan.count}", due, money(nxt.payment) if nxt else "—",
                money(st.still_to_pay) + (" *" if st.replanned else ""), str(st.last_due or "—"),
            )
        console.print(f"[bold]Loans[/] · {at}")
        console.print(t)
        console.print("[dim]Rate a month: contractual, without KKDF and BSMV. Cost a year: the annual cost rate of the terms.[/]")
        if replanned:
            console.print(
                "[dim]* The ledger's balance differs from the plan (an early repayment, or other terms): "
                "the installments left are worked out from the balance.[/]"
            )


@loan_app.command("schedule")
@guarded
def loan_schedule(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The loan's name or account")],
    csv_file: Annotated[Optional[Path], typer.Option("--csv", help="Also write the plan to this CSV file")] = None,
) -> None:
    """A loan's payment plan, with the installments paid so far."""
    import csv

    from financeproject.ledger.loans import get_loan, plan

    today = day(None)
    with ledger(ctx) as s:
        loan = get_loan(s, name)
        items = plan(loan)
        paid = {p.number: p.transaction.date for p in loan.payments if p.number is not None}
        t = table("#>", "Due", "Installment>", "Principal>", "Interest>", "KKDF>", "BSMV>", "Owed after>", "")
        for i in items:
            if i.number in paid:
                status = f"paid {paid[i.number]}"
            elif i.number < loan.start_number:
                status = "before the ledger"
            elif i.due < today:
                status = "[yellow]overdue[/]"
            else:
                status = ""
            t.add_row(
                str(i.number), str(i.due), money(i.payment), money(i.principal), money(i.interest), money(i.kkdf),
                money(i.bsmv), money(i.balance), status,
            )
        t.add_section()

        def total(attr: str) -> str:
            return f"[bold]{money(sum((getattr(i, attr) for i in items), Decimal(0)))}[/]"

        t.add_row("", "[bold]Total[/]", total("payment"), total("principal"), total("interest"), total("kkdf"), total("bsmv"), "", "")
        _print_loan(loan)
        console.print(t)
        if csv_file is not None:
            with csv_file.open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["number", "due", "installment", "principal", "interest", "kkdf", "bsmv", "owed_after", "paid_on"])
                for i in items:
                    w.writerow([i.number, i.due, i.payment, i.principal, i.interest, i.kkdf, i.bsmv, i.balance, paid.get(i.number, "")])
            console.print(f"Written: {csv_file}")


@loan_app.command("pay")
@guarded
def loan_pay(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The loan's name or account")],
    from_: Annotated[str, typer.Option("--from", help="Account paid from")],
    value: Annotated[Optional[str], typer.Option("--amount", help="Default: the planned installment")] = None,
    number: Annotated[Optional[int], typer.Option(help="The installment paid (default: the next unpaid one)")] = None,
    extra: Annotated[bool, typer.Option(help="An early repayment outside the installments: all of it lowers the principal")] = False,
    on: DateOption = None,
) -> None:
    """Pay a loan: the next installment as planned, another amount, or an early repayment."""
    from financeproject.ledger.loans import get_loan, record_payment

    if extra and number is not None:
        raise FinanceError("--extra is a payment outside the installments; leave out --number.")
    with ledger(ctx) as s:
        loan = get_loan(s, name)
        tx = record_payment(s, loan, day(on), from_, amount(value) if value else None, number, extra=extra)
        principal = sum((p.amount for p in tx.postings if p.account_id == loan.account_id), Decimal(0))
        paid = -sum((p.amount for p in tx.postings if p.amount < 0), Decimal(0))
        console.print(
            f"[green]#{tx.id}[/] {tx.date} {escape(tx.description)}: {money(paid)} = principal {money(principal)} + "
            f"interest and taxes {money(paid - principal)}"
        )


@loan_app.command("delete")
@guarded
def loan_delete(ctx: typer.Context, name: Annotated[str, typer.Argument(help="The loan's name or account")]) -> None:
    """Forget a loan's terms; its account and transactions stay."""
    from financeproject.ledger.loans import delete_loan, get_loan

    with ledger(ctx) as s:
        loan = get_loan(s, name)
        label = loan.name
        delete_loan(s, loan)
    console.print(f"Loan terms deleted: {escape(label)} (the account and its transactions are kept)")


@app.command()
@guarded
def apr(
    principal: Annotated[str, typer.Option("--amount", help="The credit you receive")],
    count: Annotated[int, typer.Option(help="Number of monthly installments")],
    installment: Annotated[Optional[str], typer.Option(help="The installment offered")] = None,
    last: Annotated[Optional[str], typer.Option(help="With --installment: a last installment that differs")] = None,
    rate: Annotated[Optional[str], typer.Option(help="Instead of --installment: the interest rate a month, per cent")] = None,
    kkdf: Annotated[str, typer.Option(help="With --rate: KKDF, per cent of the interest")] = "15",
    bsmv: Annotated[str, typer.Option(help="With --rate: BSMV, per cent of the interest")] = "15",
    method: Annotated[str, typer.Option(help="With --rate: annuity or equal-principal")] = "annuity",
    fees: Annotated[str, typer.Option(help="Paid at the start: file fee, insurance, account fee")] = "0",
    first_in: Annotated[int, typer.Option(help="Months until the first installment")] = 1,
) -> None:
    """The cost of a credit offer: its annual cost rate (yıllık maliyet oranı).

    The rate at which the credit received equals the installments and fees
    paid, all discounted to the same day (Directive 2008/48/EC Annex I; the
    Turkish consumer credit regulation uses the same formula).
    """
    from datetime import date as date_

    from financeproject.core.loans import (
        ANNUITY,
        EQUAL_PRINCIPAL,
        LoanTerms,
        offer_cost_rate,
        schedule,
    )

    if count < 1 or first_in < 0:
        raise FinanceError("--count must be at least 1 and --first-in at least 0.")
    if (rate is None) == (installment is None):
        raise FinanceError("Give either the installment (--installment) or the monthly interest rate (--rate).")
    p = amount(principal)
    if installment is not None:
        payments = [amount(installment)] * count
        if last is not None:
            payments[-1] = amount(last)
        how = f"{count} installments of {money(payments[0])}" + (f" (the last {money(payments[-1])})" if last else "")
    else:
        if last is not None:
            raise FinanceError("--last goes with --installment.")
        methods = {"annuity": ANNUITY, "equal-principal": EQUAL_PRINCIPAL}
        _check_choice(method, tuple(methods), "--method")
        try:
            terms = LoanTerms(p, _percent(rate, "--rate"), count, date_(2001, 1, 1), methods[method], _percent(kkdf, "--kkdf"), _percent(bsmv, "--bsmv"))
        except ValueError as exc:
            raise FinanceError(str(exc)) from exc
        payments = [i.payment for i in schedule(terms)]
        how = (
            f"{count} installments at {_rate(terms.monthly_rate, 3)} a month with KKDF {_rate(terms.kkdf, 0)} and BSMV "
            f"{_rate(terms.bsmv, 0)}: {money(payments[0])}" + (f" … {money(payments[-1])}" if payments[-1] != payments[0] else " each")
        )
    fee = amount(fees)
    try:
        x = offer_cost_rate(p, payments, fee, first_in)
    except ValueError as exc:
        raise FinanceError(str(exc)) from exc
    total = sum(payments, Decimal(0)) + fee
    monthly = (1 + x) ** (1 / 12) - 1
    console.print(f"Credit {money(p)}; {how}, the first in {first_in} month{'s' if first_in != 1 else ''}" + (f"; fees {money(fee)}" if fee else ""))
    console.print(f"Paid back in all: {money(total)} (cost {money(total - p)})")
    console.print(f"Annual cost rate (yıllık maliyet oranı): [bold]{_rate(x)}[/]")
    console.print(f"Cost a month: {_rate(monthly, 3)}; a year {_rate(monthly * 12)} simple (× 12), {_rate(x)} compounded")


@app.command("cash-or-installments")
@guarded
def cash_or_installments_cmd(
    ctx: typer.Context,
    cash: Annotated[str, typer.Option(help="The price if you pay now")],
    installment: Annotated[str, typer.Option(help="One installment")],
    count: Annotated[int, typer.Option(help="Number of installments")],
    first_in: Annotated[int, typer.Option(help="Months until the first installment (0: the first is paid now)")] = 1,
    rate: Annotated[Optional[str], typer.Option(help="What your money earns meanwhile, per cent a month (e.g. a deposit after tax)")] = None,
    annual_rate: Annotated[Optional[str], typer.Option(help="The same, per cent a year")] = None,
) -> None:
    """Pay cash or in installments? Compares them in today's money.

    The installments are discounted at what the money earns while it stays
    with you (--rate or --annual-rate). Without either, at the CPI's inflation
    over the last twelve months: a comparison in real terms.
    """
    from financeproject.core.loans import (
        cash_or_installments,
        effective_annual,
        monthly_from_annual,
    )
    from financeproject.reports.debt import trailing_inflation

    if rate is not None and annual_rate is not None:
        raise FinanceError("Give --rate or --annual-rate, not both.")
    if rate is not None:
        monthly, source = float(_percent(rate, "--rate")), "given"
    elif annual_rate is not None:
        monthly, source = monthly_from_annual(float(_percent(annual_rate, "--annual-rate"))), "given"
    else:
        with ledger(ctx) as s:
            expected = trailing_inflation(s)
        if expected is None:
            raise FinanceError("No CPI in the ledger: give what your money earns with --rate or --annual-rate (or run 'finance cpi').")
        monthly, source = monthly_from_annual(expected.rate), f"inflation, {expected.source}"
    try:
        r = cash_or_installments(amount(cash), amount(installment), count, monthly, first_in)
    except ValueError as exc:
        raise FinanceError(str(exc)) from exc
    when = "the first now" if first_in == 0 else f"the first in {first_in} month{'s' if first_in != 1 else ''}"
    console.print(f"Cash {money(r.cash_price)} now, or {count} × {money(r.installment)} = {money(r.total)}, {when}.")
    if r.implied_rate > 1e-9:
        console.print(
            f"The installments charge {_rate(r.implied_rate)} a month ({_rate(effective_annual(r.implied_rate))} a year) over the cash price."
        )
    elif r.implied_rate < -1e-9:
        console.print(f"The installments come to less than the cash price: a discount of {_rate(-r.implied_rate)} a month.")
    else:
        console.print("The installments add up to the cash price: no interest.")
    console.print(f"Money kept meanwhile: {_rate(monthly)} a month ({_rate(effective_annual(monthly))} a year; {source}).")
    verdict = "the installments are cheaper" if r.installments_win else "cash is cheaper"
    gap = abs(Decimal(repr(r.advantage))).quantize(Decimal("0.01"))
    console.print(
        f"In today's money the installments cost {money(r.present_value)}: {money(gap)} "
        f"{'less' if r.installments_win else 'more'} than cash → [bold]{verdict}[/]."
    )


@app.command()
@guarded
def payoff(
    ctx: typer.Context,
    budget: Annotated[str, typer.Option(help="What you can pay towards debts each month")],
    rate: Annotated[
        Optional[list[str]], typer.Option(help="ACCOUNT=PER-CENT-A-MONTH for a debt that is not a loan (repeatable)")
    ] = None,
    minimum: Annotated[Optional[list[str]], typer.Option(help="ACCOUNT=AMOUNT: that debt's least monthly payment (repeatable)")] = None,
    on: DateOption = None,
) -> None:
    """Plan paying off debts: highest rate first (avalanche) or smallest debt first (snowball).

    Takes the loans defined in the ledger (their rate with taxes, their
    installment as the least payment) and the debts named with --rate or
    --minimum. Every month each debt gets its least payment and the rest of the
    budget goes to one debt at a time; a debt paid off frees its payment for the
    next. Early repayment fees are not counted.
    """
    from financeproject.core.loans import AVALANCHE, LEAST, SNOWBALL
    from financeproject.core.loans import payoff as simulate
    from financeproject.reports.debt import payoff_debts

    b = amount(budget)
    at = day(on)
    with ledger(ctx) as s:
        debts = payoff_debts(
            s, at, _assignments(rate, "--rate", lambda v: _percent(v, "--rate")), _assignments(minimum, "--minimum", amount)
        )
    if not debts:
        raise FinanceError("No debts to plan: define loans with 'finance loan add', or name debts with --rate/--minimum.")
    least = sum((d.minimum for d in debts), Decimal(0))
    if b < least:
        raise FinanceError(f"The budget {money(b)} does not cover the least payments, {money(least)} a month.")
    t = table("Debt", "Owed>", "Rate a month>", "Least payment>")
    for d in debts:
        t.add_row(escape(d.name), money(d.balance), _rate(d.monthly_rate, 3), money(d.minimum))
    console.print(f"[bold]Debts[/] · {at} · rates include KKDF and BSMV")
    console.print(t)
    plans = [("As now: least payments only", simulate(debts, least, LEAST))]
    plans += [
        ("Avalanche: highest rate first", simulate(debts, b, AVALANCHE)),
        ("Snowball: smallest debt first", simulate(debts, b, SNOWBALL)),
    ]
    p = table("Plan", "Debt-free in>", "Interest and taxes>", "Order (month paid off)")
    for label, result in plans:
        months = f"{result.months} months" if result.feasible else "never"
        order = ", ".join(f"{escape(n)} ({result.paid_off[n]})" for n in result.order)
        p.add_row(label, months, money(result.interest) if result.feasible else "—", order)
    console.print(f"[bold]Budget[/] {money(b)} a month: least payments {money(least)} + extra {money(b - least)}")
    console.print(p)
    avalanche, snowball = plans[-2][1], plans[-1][1]
    if avalanche.feasible and snowball.feasible:
        saving = snowball.interest - avalanche.interest
        if saving > 0:
            console.print(f"Avalanche saves {money(saving)} in interest; snowball clears a first debt sooner, which helps some people keep going.")
        else:
            console.print("Both orders cost the same here.")
    console.print("[dim]Interest is charged monthly on what is owed; extra payments lower the principal at no fee.[/]")


@app.command()
@guarded
def debt(
    ctx: typer.Context,
    on: DateOption = None,
    inflation: Annotated[
        Optional[str], typer.Option(help="Expected inflation, per cent a year (default: the CPI's last twelve months)")
    ] = None,
) -> None:
    """Every debt: what is owed, what it costs, and what the installments left are worth after inflation."""
    from financeproject.reports.debt import debt_report

    at = day(on)
    with ledger(ctx) as s:
        report = debt_report(s, at, float(_percent(inflation, "--inflation")) if inflation is not None else None)
    if not report.lines:
        console.print(f"No debts on {at}.")
        return
    t = table("Debt", "Owed>", "Rate a month>", "Cost a year>", "Real a year>", "Next", "Left to pay>", "In today's money>", "Last")
    for line in report.lines:
        st = line.state
        unit = "" if line.commodity == "TRY" else f" {line.commodity}"
        if st is None:
            t.add_row(escape(line.account), money(line.owed) + unit, "—", "—", "—", "—", "—", "—", "")
            continue
        nxt = f"{st.next.due} {money(st.next.payment)}" if st.next else "—"
        t.add_row(
            escape(st.loan.name), money(line.owed) + unit, _rate(st.loan.monthly_rate, 3), _rate(line.cost_rate),
            _rate(line.real_rate), nxt, money(st.still_to_pay) + (" *" if st.replanned else ""),
            money(line.today_value) if line.today_value is not None else "—", str(st.last_due or "—"),
        )
    t.add_section()
    t.add_row(
        "[bold]Total (TRY)[/]", f"[bold]{money(report.owed)}[/]", "", "", "", "", f"[bold]{money(report.still_to_pay)}[/]",
        f"[bold]{money(report.today_value)}[/]" if report.today_value is not None else "—", "",
    )
    infl = f"inflation {_rate(report.inflation.rate, 1)} a year ({report.inflation.source})" if report.inflation else "no inflation figure"
    console.print(f"[bold]Debts[/] · {at} · {infl}")
    console.print(t)
    for line in report.lines:
        if line.later_installments:
            console.print(
                f"{escape(line.account)}: {money(line.later_installments)} of the {money(line.owed)} owed are installments "
                "billed in later months."
            )
    if report.inflation and report.today_value is not None and report.still_to_pay:
        console.print(
            f"Inflation lightens the loans: {money(report.still_to_pay)} still to pay is worth {money(report.today_value)} in today's money."
        )
    console.print(
        "[dim]Rate a month: contractual, without KKDF and BSMV. Cost a year: annual cost rate of the terms. "
        "Real a year: interest and taxes compounded, after inflation.[/]"
    )


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
