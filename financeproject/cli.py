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
for sub, name in (
    (account_app, "account"),
    (txn_app, "txn"),
    (rule_app, "rule"),
    (price_app, "price"),
    (commodity_app, "commodity"),
    (person_app, "person"),
    (config_app, "config"),
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
) -> None:
    """Spending: the expense grows, the paying account shrinks (on a credit card the debt grows)."""
    with ledger(ctx) as s:
        e = acc_mod.find_account(s, expense_account)
        tx = record_expense(s, amount(value), e, from_, day(on), description or e.path.split(":")[-1], payee)
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


@app.command("import")
@guarded
def import_(
    ctx: typer.Context,
    file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="CSV file")],
    account: Annotated[Optional[str], typer.Option(help="Account of every row when the file has no 'account' column")] = None,
    delimiter: Annotated[str, typer.Option(help="Column delimiter: ';', ',', 'tab' or 'auto'")] = "auto",
    decimal: Annotated[str, typer.Option(help="Decimal separator: ',', '.' or 'auto'")] = "auto",
    date_format: Annotated[Optional[str], typer.Option(help="For example %d.%m.%Y (default: 2026-10-09 or 09.10.2026)")] = None,
    encoding: Annotated[Optional[str], typer.Option(help="utf-8, cp1254… (default: detected)")] = None,
    columns: Annotated[
        Optional[str], typer.Option(help="Mapping, e.g. 'date=Transaction Date,description=Details,amount=Amount'")
    ] = None,
    skip: Annotated[int, typer.Option(help="Lines to skip before the header row")] = 0,
    invert_sign: Annotated[bool, typer.Option(help="For statements that list spending as positive amounts")] = False,
    dry_run: Annotated[bool, typer.Option(help="Preview without writing anything")] = False,
) -> None:
    """Import a CSV file; rows already imported are skipped. Format: README → Importing."""
    from financeproject.ingest.csvfile import CsvFormat, import_csv, parse_column_map

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
        result = import_csv(s, file, fmt, account=account, dry_run=dry_run)
        t = table("Line>", "Date", "Description", "Amount>", "Account", "Counter account", "How", "")
        for r in result.rows[:200]:
            t.add_row(
                str(r.line),
                str(r.date),
                escape(r.description[:40]),
                f"{money(r.amount)} {r.commodity}",
                escape(r.account),
                escape(r.counter_account),
                r.how,
                "[dim]already in[/]" if r.duplicate else "",
            )
        console.print(t)
        if len(result.rows) > 200:
            console.print(f"… {len(result.rows) - 200} more rows")
        console.print(f"Encoding: {result.encoding}")
        if result.errors:
            for e in result.errors:
                err.print(f"[red]{escape(e)}[/]")
            raise FinanceError(f"{len(result.errors)} rows have errors; nothing was written.")
        new = len(result.rows) - result.duplicates
        if dry_run:
            console.print(f"[yellow]Dry run:[/] {new} new, {result.duplicates} already in; nothing was written.")
        else:
            console.print(
                f"[green]{result.added} transactions added[/], {result.duplicates} rows were already in, "
                f"{result.uncategorized} uncategorized (add rules, then run 'finance rule apply')."
            )


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


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
