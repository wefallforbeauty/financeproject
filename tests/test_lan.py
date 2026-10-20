"""Phones and the wall monitor on the home network (04c).

The household's password, paired devices and pairing codes; the certificates; both sides of the guard;
pairing as a person does it; what a phone and a wall monitor may open; guest mode; and the HTTPS server
itself. The home-network side runs in the test process like the rest of the interface (FastAPI's test
client, with a client address on a made-up home network); one test starts the real HTTPS server on
127.0.0.1 and checks its certificate chain.
"""

from __future__ import annotations

import ipaddress
import json
import ssl
import stat
from datetime import UTC, datetime, timedelta
from html import unescape

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509 import verification
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from financeproject import config
from financeproject.cli import app as cli_app
from financeproject.errors import FinanceError
from financeproject.web import devices, lan, server, tls
from financeproject.web.devices import DeviceStore, PairingError, Pairings
from financeproject.web.lan import Lan
from financeproject.web.security import DEVICE_COOKIE, csrf_for
from tests.test_web import (  # noqa: F401 - fixtures used by name
    CSRF,
    demo_file,
    demo_web,
    ledger,
    post,
    strict_templates,
    web,
)

SHARED_FIXTURES = (demo_file, demo_web, ledger, strict_templates, web)
"""The interface tests' fixtures, which these tests take by name."""
HOME = Lan("192.168.1.23", 8766, ("box", "box.local"))
HOME_URL = "https://192.168.1.23:8766"
PHONE_ADDRESS = ("192.168.1.50", 50123)
PASSWORD = "correct horse battery"


class Clock:
    """A clock the tests move by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def home(web):
    """The test interface opened to a made-up home network, with the household's password set."""
    web.app.state.lan = HOME
    web.app.state.devices.set_password(PASSWORD)
    return web


def visitor(app) -> TestClient:
    return TestClient(app, base_url=HOME_URL, client=PHONE_ADDRESS)


def device_post(device: TestClient, url: str, data: dict | None = None, **kwargs):
    token = device.cookies.get(DEVICE_COOKIE)
    return device.post(
        url, data={"csrf_token": csrf_for(token), **(data or {})}, headers={"Origin": HOME_URL}, follow_redirects=False, **kwargs
    )


def pair(computer: TestClient, kind: str = "phone", name: str = "Test phone") -> TestClient:
    """A device paired the way a person does it: the computer makes a code, the device gives it and the password."""
    assert post(computer, "/settings/devices/pair", {"kind": kind}).status_code == 303
    code = computer.app.state.pairings.current.code
    device = visitor(computer.app)
    page = device.get("/pair", params={"code": code}).text
    assert 'action="/pair"' in page and 'name="password"' in page
    answer = device.post(
        "/pair", data={"code": code, "name": name, "password": PASSWORD}, headers={"Origin": HOME_URL}, follow_redirects=False
    )
    assert answer.status_code == 303, answer.text[:300]
    return device


# --------------------------------------------------------------------------- the password and the devices


def test_the_password_is_kept_as_an_scrypt_hash():
    stored = devices.hash_password("secret words", salt=b"0" * 16, n=2**10)
    scheme, n, r, p, salt, digest = stored.split("$")
    assert (scheme, n, r, p) == ("scrypt", "1024", "8", "1") and "secret" not in stored
    assert devices.verify_password("secret words", stored) and not devices.verify_password("secret word", stored)
    for broken in (None, "", "scrypt$1$2", "md5$1$8$1$AA==$AA==", "scrypt$x$8$1$AA==$AA=="):
        assert not devices.verify_password("secret words", broken)
    store = DeviceStore()
    assert not store.has_password and not store.check_password("anything")
    with pytest.raises(FinanceError, match="at least 8"):
        store.set_password("short")
    store.set_password(PASSWORD)
    assert store.check_password(PASSWORD) and not store.check_password(PASSWORD + "!")
    assert stat.S_IMODE(devices.devices_path().stat().st_mode) == 0o600
    assert PASSWORD not in devices.devices_path().read_text()
    assert DeviceStore().check_password(PASSWORD)  # read back from the file


def test_devices_keep_only_a_hash_of_their_token():
    store = DeviceStore()
    phone, token = store.add("", devices.PHONE)
    second, _ = store.add("  ", devices.PHONE)
    monitor, monitor_token = store.add("Hall " * 20, devices.MONITOR)
    assert (phone.name, second.name) == ("Phone", "Phone 2") and len(monitor.name) <= devices.MAX_NAME
    assert store.add("", devices.PHONE, default="Telefon")[0].name == "Telefon"
    assert store.authenticate(token) == phone and store.authenticate(monitor_token).is_monitor
    assert store.authenticate("") is None and store.authenticate(token + "x") is None
    text = devices.devices_path().read_text()
    assert token not in text and devices.token_hash(token) in text
    assert store.remove(phone.id) == phone and store.authenticate(token) is None and store.remove(phone.id) is None
    with pytest.raises(FinanceError):
        store.add("x", "toaster")
    assert store.remove_all() == 3 and store.devices == []


def test_last_seen_is_saved_now_and_then_and_a_removed_device_stays_removed():
    clock = Clock()
    store = DeviceStore()
    phone, token = store.add("Phone", devices.PHONE)
    store.seen(phone, "192.168.1.50", clock)
    assert json.loads(devices.devices_path().read_text())["devices"][0]["last_address"] == "192.168.1.50"
    clock.now += 10
    store.seen(phone, "192.168.1.51", clock)  # in memory only: saved at most every few minutes
    assert json.loads(devices.devices_path().read_text())["devices"][0]["last_address"] == "192.168.1.50"
    assert store.devices[0].last_address == "192.168.1.51"
    DeviceStore().remove_all()  # another process (finance ui --forget-devices)
    assert store.authenticate(token) is None
    clock.now += devices.SEEN_EVERY
    store.seen(phone, "192.168.1.50", clock)
    assert DeviceStore().devices == []


def test_pairing_codes_are_single_use_short_lived_and_limited():
    clock = Clock()
    store = DeviceStore()
    store.set_password(PASSWORD)
    pairings = Pairings(clock)
    code = pairings.start(devices.PHONE).code
    assert len(code) == devices.CODE_LENGTH and set(code) <= set(devices.ALPHABET)
    assert pairings.kind_of(devices.show_code(code).lower()) == devices.PHONE and pairings.kind_of("nope") is None
    with pytest.raises(PairingError, match="wrong"):
        pairings.redeem(code, "not the password", "", store)
    with pytest.raises(PairingError, match="wrong"):
        pairings.redeem("A" * devices.CODE_LENGTH, PASSWORD, "", store)
    device, token = pairings.redeem(devices.show_code(code), PASSWORD, "Mine", store)
    assert device.name == "Mine" and store.authenticate(token) == device and pairings.current.device == device
    with pytest.raises(PairingError, match="expired or was used"):
        pairings.redeem(code, PASSWORD, "", store)
    clock.now += devices.CODE_LIFETIME + 1
    assert pairings.current is None  # the note of the device it paired goes when the code would have expired
    # five wrong tries end a code
    code = pairings.start(devices.MONITOR).code
    for _ in range(devices.CODE_TRIES):
        with pytest.raises(PairingError):
            pairings.redeem(code, "wrong", "", store)
    assert pairings.current is None
    # a code runs out
    code = pairings.start(devices.PHONE).code
    assert pairings.seconds_left(pairings.current) == devices.CODE_LIFETIME
    clock.now += devices.CODE_LIFETIME + 1
    assert pairings.current is None and pairings.kind_of(code) is None
    with pytest.raises(PairingError, match="expired"):
        pairings.redeem(code, PASSWORD, "", store)
    # ten wrong tries in ten minutes stop pairing, even with a new code
    for _ in range(devices.FAILURE_LIMIT):
        code = pairings.start(devices.PHONE).code
        with pytest.raises(PairingError):
            pairings.redeem(code, "wrong", "", store)
    code = pairings.start(devices.PHONE).code
    with pytest.raises(PairingError, match="Too many"):
        pairings.redeem(code, PASSWORD, "", store)
    clock.now += devices.FAILURE_WINDOW
    assert pairings.redeem(code, PASSWORD, "", store)[0].kind == devices.PHONE
    with pytest.raises(FinanceError):
        pairings.start("toaster")


# --------------------------------------------------------------------------- the certificates and the network


def _verify(where: tls.Paths, name: x509.GeneralName) -> None:
    store = verification.Store([x509.load_pem_x509_certificate(where.ca.read_bytes())])
    leaf = x509.load_pem_x509_certificate(where.server.read_bytes())
    verification.PolicyBuilder().store(store).build_server_verifier(name).verify(leaf, [])


def test_certificates_chain_constraints_and_renewal(tmp_path):
    home_ip = ipaddress.ip_address("192.168.1.23")
    where = tls.ensure("box", [home_ip], tmp_path)
    for name in (x509.IPAddress(home_ip), x509.DNSName("box.local"), x509.DNSName("box")):
        _verify(where, name)
    with pytest.raises(verification.VerificationError):
        _verify(where, x509.IPAddress(ipaddress.ip_address("192.168.1.99")))
    assert stat.S_IMODE(where.ca_key.stat().st_mode) == 0o600 and stat.S_IMODE(where.server_key.stat().st_mode) == 0o600
    ca = x509.load_pem_x509_certificate(where.ca.read_bytes())
    key = serialization.load_pem_private_key(where.ca_key.read_bytes(), password=None)
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.path_length == 0
    # the authority cannot vouch for a name on the internet
    outside, _ = tls.make_server(ca, key, "example", [ipaddress.ip_address("8.8.8.8")])
    store = verification.Store([ca])
    with pytest.raises(verification.VerificationError, match="name constraint"):
        verification.PolicyBuilder().store(store).build_server_verifier(x509.IPAddress(ipaddress.ip_address("8.8.8.8"))).verify(outside, [])
    # the same names: nothing is made again; a new address: a new server certificate from the same authority
    before = where.server.read_bytes()
    assert tls.ensure("box", [home_ip], tmp_path).server.read_bytes() == before
    tls.ensure("box", [home_ip, ipaddress.ip_address("10.0.0.5")], tmp_path)
    assert where.server.read_bytes() != before and where.ca.read_bytes() == ca.public_bytes(serialization.Encoding.PEM)
    _verify(where, x509.IPAddress(ipaddress.ip_address("10.0.0.5")))
    # a certificate that is about to run out is renewed
    old, old_key = tls.make_server(ca, key, "box", [home_ip], now=datetime.now(UTC) - timedelta(days=tls.SERVER_DAYS - 10))
    where.server.write_bytes(old.public_bytes(serialization.Encoding.PEM))
    tls.ensure("box", [home_ip], tmp_path)
    renewed = x509.load_pem_x509_certificate(where.server.read_bytes())
    assert renewed.not_valid_after_utc - datetime.now(UTC) > timedelta(days=tls.SERVER_DAYS - 2)
    # a new computer name needs a new authority (its constraints name the old one)
    tls.ensure("newbox", [home_ip], tmp_path)
    assert where.ca.read_bytes() != ca.public_bytes(serialization.Encoding.PEM)
    _verify(where, x509.DNSName("newbox.local"))
    assert tls.fingerprint(tmp_path).count(":") == 31 and tls.authority_der(tmp_path)[:1] == b"\x30"
    assert tls.fingerprint(tmp_path / "none") is None and tls.authority_der(tmp_path / "none") is None
    with pytest.raises(ValueError):
        tls.ensure("box", [], tmp_path)


def test_home_addresses():
    for address in ("192.168.1.5", "10.1.2.3", "172.20.0.1", "169.254.3.4", "127.0.0.1", "::ffff:192.168.1.5"):
        assert lan.is_home(address), address
    for address in ("8.8.8.8", "100.64.0.1", "2001:db8::1", "testclient", ""):
        assert not lan.is_home(address), address
    assert HOME.url == HOME_URL and "box.local:8766" in HOME.hosts and "https://192.168.1.23:8766" in HOME.origins
    found = lan.addresses()
    assert all(lan.is_home(a) and not a.is_loopback for a in found)
    assert found == sorted(found, key=lambda a: 0 if a in ipaddress.ip_network("192.168.0.0/16") else 1)
    assert lan.hostname() == lan.hostname().lower() and " " not in lan.hostname()


# --------------------------------------------------------------------------- the guard on the home network


def test_an_unpaired_device_sees_only_pairing(home, tmp_path):
    phone = visitor(home.app)
    page = phone.get("/")
    assert page.status_code == 401 and "This device is not paired" in page.text and 'lang="tr"' in page.text
    assert phone.get("/transactions").status_code == 401 and phone.get("/monitor").status_code == 401
    assert phone.get("/static/css/app.css").status_code == 200 and phone.get("/ping").text == "financeproject"
    assert phone.get("/pair").status_code == 200 and "expired or was used" in phone.get("/pair").text
    assert phone.get("/ca.crt").status_code == 404  # made with the server's certificate
    tls.ensure("box", [ipaddress.ip_address("192.168.1.23")])
    answer = phone.get("/ca.crt")
    assert answer.status_code == 200 and answer.headers["content-type"] == "application/x-x509-ca-cert"
    assert x509.load_der_x509_certificate(answer.content).extensions.get_extension_for_class(x509.NameConstraints)
    # a visitor must send this server's origin with a change
    assert phone.post("/pair", data={"code": "x", "password": "y"}, follow_redirects=False).status_code == 403
    assert phone.post("/pair", data={"code": "x"}, headers={"Origin": "https://evil.example"}, follow_redirects=False).status_code == 403
    # only home addresses, only this server's names
    outside = TestClient(home.app, base_url=HOME_URL, client=("8.8.8.8", 1))
    assert outside.get("/pair").status_code == 403
    assert phone.get("/pair", headers={"Host": "evil.example"}).status_code == 400
    assert TestClient(home.app, base_url="https://box.local:8766", client=PHONE_ADDRESS).get("/pair").status_code == 200
    # the computer's own token does not open the home-network side
    phone.cookies.set("fp_session_8765", "token-for-the-tests-" + "x" * 23)
    assert phone.get("/").status_code == 401


def test_pairing_a_phone(home, ledger):
    computer = home
    assert "Open to the home network" not in computer.get("/settings/devices").text  # it is open
    assert post(computer, "/settings/devices/pair", {"kind": "toaster"}).status_code == 400
    assert post(computer, "/settings/devices/pair", {"kind": "phone"}).status_code == 303
    page = computer.get("/settings/devices").text
    code = computer.app.state.pairings.current.code
    assert '<svg viewBox="0 0' in page and f"{HOME_URL}/pair?code={code}" in page and devices.show_code(code) in page
    part = computer.get("/settings/devices/pairing", headers={"HX-Request": "true"})
    assert part.status_code == 200 and "HX-Refresh" not in part.headers and 'hx-trigger="every 3s"' in part.text
    phone = visitor(computer.app)
    wrong = phone.post("/pair", data={"code": code, "password": "nope", "name": "P"}, headers={"Origin": HOME_URL}, follow_redirects=False)
    assert wrong.status_code == 400 and "The code or the password is wrong." in wrong.text and 'value="P"' in wrong.text
    answer = phone.post("/pair", data={"code": devices.show_code(code), "password": PASSWORD, "name": " My  phone "},
                        headers={"Origin": HOME_URL}, follow_redirects=False)
    assert answer.status_code == 303 and answer.headers["location"] == "/"
    cookie = answer.headers["set-cookie"].lower()
    assert all(part in cookie for part in ("fp_device=", "httponly", "secure", "samesite=lax", "max-age=34560000"))
    overview = phone.get("/").text
    assert "My phone is paired. Welcome!" in overview and "This device: My phone" in overview
    assert "is paired. Welcome!" not in phone.get("/").text  # shown once
    # the computer's page notices and shows the device
    part = computer.get("/settings/devices/pairing", headers={"HX-Request": "true"})
    assert part.headers.get("HX-Refresh") == "true" and "My phone is paired." in part.text
    page = computer.get("/settings/devices").text
    assert "My phone" in page and "192.168.1.50" in page
    assert phone.get("/pair").status_code == 200 and phone.get("/pair", follow_redirects=False).headers["location"] == "/"
    # the code was used: a second device cannot pair with it
    other = visitor(computer.app)
    again = other.post("/pair", data={"code": code, "password": PASSWORD}, headers={"Origin": HOME_URL}, follow_redirects=False)
    assert again.status_code == 400 and "expired or was used" in again.text
    # on the computer, /pair leads to Settings
    assert computer.get("/pair", follow_redirects=False).headers["location"] == "/settings/devices#pair"


def test_what_a_phone_may_do(home, ledger):
    phone = pair(home)
    settings = phone.get("/settings").text
    assert "This device" in settings and "/settings/backup" not in settings and "/quit" not in settings and "Display" in settings
    for page in ("/settings/devices", "/settings/backups/x.sqlite", "/settings/devices/certificate"):
        assert phone.get(page).status_code == 403, page
    assert device_post(phone, "/quit").status_code == 403 and device_post(phone, "/settings/ledger", {"which": "demo"}).status_code == 403
    for page in ("/", "/transactions", "/reports/balance-sheet", "/debts", "/investments", "/monitor", "/transactions/new"):
        assert phone.get(page).status_code == 200, page
    # changes need the phone's own token and this origin
    assert device_post(phone, "/settings/display", {"language": "tr"}).status_code == 303
    assert config.language() == "tr"
    assert phone.post("/settings/display", data={"csrf_token": CSRF, "language": "en"}, headers={"Origin": HOME_URL},
                      follow_redirects=False).status_code == 403
    token = phone.cookies.get(DEVICE_COOKIE)
    assert phone.post("/settings/display", data={"csrf_token": csrf_for(token), "language": "en"},
                      headers={"Origin": "https://evil.example"}, follow_redirects=False).status_code == 403
    # each side keeps its own messages
    assert device_post(phone, "/settings/display", {"language": "en"}).status_code == 303
    assert "Saved." not in home.get("/settings").text and "Saved." in phone.get("/settings").text
    # the page tells the phone's token, not the computer's
    assert csrf_for(token) in phone.get("/").text and CSRF not in phone.get("/").text
    # a removed phone is out at once
    device = home.app.state.devices.devices[0]
    assert post(home, f"/settings/devices/{device.id}/remove").status_code == 303
    assert phone.get("/").status_code == 401
    assert post(home, f"/settings/devices/{device.id}/remove").status_code == 404


def test_a_wall_monitor_and_guest_mode(home, ledger):
    monitor = pair(home, "monitor", "Hall")
    assert monitor.get("/", follow_redirects=False).headers["location"] == "/monitor"
    assert monitor.get("/transactions", follow_redirects=False).headers["location"] == "/monitor"
    assert device_post(monitor, "/settings/display", {"language": "tr"}).status_code == 403
    page = monitor.get("/monitor").text
    assert "Hall is paired. Welcome!" in page and "58,950" in page and "Guests are here: hide the amounts" in page
    assert monitor.get("/monitor/panel").text.startswith('<div id="monitor-panel"')
    assert device_post(monitor, "/monitor/guests", {"guests": "on"}).status_code == 303
    hidden = monitor.get("/monitor").text
    assert "58,950" not in hidden and "•••" in hidden and "Guest mode: amounts and payees are hidden." in hidden
    assert "Show the amounts" not in hidden and "Turn it off from a phone or the computer." in hidden
    assert '"percent": true' in hidden
    assert device_post(monitor, "/monitor/guests", {"guests": "off"}).status_code == 403  # a monitor cannot turn it off
    phone = pair(home)
    assert "Turn guest mode off" in phone.get("/settings").text
    assert device_post(phone, "/monitor/guests", {"guests": "off", "next": "//evil.example"}).headers["location"] == "/monitor"
    assert "58,950" in monitor.get("/monitor").text
    assert post(home, "/monitor/guests", {"guests": "on", "next": "/settings/devices#monitor"}).headers["location"] == "/settings/devices#monitor"
    assert "Show the amounts" in home.get("/monitor").text  # the computer may show them again


def test_the_monitor_notices_guest_mode_and_keeps_its_chart(home, ledger):
    monitor = pair(home, "monitor", "Hall")
    panel = monitor.get("/monitor/panel").text
    assert "data-json='{" in panel and "<script" not in panel  # htmx leaves script elements out of a swap
    assert 'hx-get="/monitor/check?guests=off"' in panel
    quiet = monitor.get("/monitor/check", params={"guests": "off"})
    assert quiet.status_code == 204 and "HX-Refresh" not in quiet.headers
    config.save_setting("monitor", "guests", "on")  # from a phone or the computer
    changed = monitor.get("/monitor/check", params={"guests": "off"})
    assert changed.status_code == 204 and changed.headers["HX-Refresh"] == "true"
    assert 'hx-get="/monitor/check?guests=on"' in monitor.get("/monitor/panel").text


def test_a_phone_cannot_log_in_as_the_computer_or_create_a_ledger(home, ledger):
    phone = pair(home)
    assert phone.get("/login", params={"token": "token-for-the-tests-" + "x" * 23}).status_code == 403
    assert phone.get("/welcome").status_code == 403 and device_post(phone, "/welcome/create").status_code == 403


def test_times_are_shown_in_this_computers_time_zone():
    from financeproject.web.app import local_time
    from financeproject.web.views.devices import seen_text

    moment = datetime(2026, 10, 20, 9, 30, tzinfo=UTC)
    assert local_time(moment) == moment.astimezone().strftime("%Y-%m-%d %H:%M")
    assert local_time(moment.replace(tzinfo=None)) == local_time(moment)  # the ledger keeps UTC without a zone
    assert seen_text(moment.isoformat()) == local_time(moment) and seen_text(None) == "—"


def test_guest_mode_hides_payees_on_the_demo(demo_web):
    page = demo_web.get("/monitor").text
    assert "The next two weeks" in page
    config.save_setting("monitor", "guests", "on")
    hidden = demo_web.get("/monitor").text
    for payee in ("Landlord", "SAMPLE EMPLOYER", "STREAMING", "TRY</span>"):
        assert payee not in hidden, payee


@pytest.mark.parametrize("language", ["en", "tr"])
def test_the_new_pages_render(home, language):
    config.save_setting("display", "language", language)
    for page in ("/settings/devices", "/settings", "/monitor", "/monitor/panel", "/settings/devices/pairing"):
        answer = home.get(page)
        assert answer.status_code == 200, (page, answer.text[:300])
    assert post(home, "/settings/devices/pair", {"kind": "monitor"}).status_code == 303
    assert home.get("/settings/devices").status_code == 200
    phone = pair(home, "monitor", "Ekran")
    assert phone.get("/monitor").status_code == 200
    assert visitor(home.app).get("/pair", params={"code": "nothing"}).status_code == 200


def test_settings_for_the_home_network(web, monkeypatch):
    page = unescape(web.get("/settings/devices").text)
    assert "Set the household's password first" in page and "disabled" in page
    assert "Phones and monitor" in web.get("/settings").text
    for form, message in (
        ({"password": "abcdefgh", "repeat": "abcdefgx"}, "The two passwords differ."),
        ({"password": "short", "repeat": "short"}, "at least 8 characters"),
    ):
        post(web, "/settings/devices/password", form)
        assert message in web.get("/settings/devices").text
    assert post(web, "/settings/devices/open").status_code == 303
    assert "Set a password first" in web.get("/settings/devices").text
    assert post(web, "/settings/devices/password", {"password": PASSWORD, "repeat": PASSWORD}).status_code == 303
    assert "Password saved." in web.get("/settings/devices").text
    monkeypatch.setattr(lan, "addresses", lambda: [])
    post(web, "/settings/devices/open")
    assert "no home-network address" in web.get("/settings/devices").text
    assert post(web, "/settings/devices/pair", {"kind": "phone"}).headers["location"] == "/settings/devices#network"
    assert post(web, "/settings/devices/start", {"open": "on"}).status_code == 303 and config.get_setting("network", "open") == "on"
    assert post(web, "/settings/devices/start").status_code == 303 and config.get_setting("network", "open") == "off"
    assert web.get("/settings/devices/certificate").status_code == 404


def test_the_https_server(web, monkeypatch):
    """Opened from Settings: a real HTTPS server on 127.0.0.1 (standing in for the home address), whose
    certificate chains to the authority; closed again from Settings."""
    port = server.free_port(0)
    config.save_setting("network", "port", str(port))
    monkeypatch.setattr(lan, "addresses", lambda: [ipaddress.ip_address("127.0.0.1")])
    post(web, "/settings/devices/password", {"password": PASSWORD, "repeat": PASSWORD})
    assert post(web, "/settings/devices/open", {"address": "127.0.0.1"}).status_code == 303
    try:
        home = web.app.state.lan
        assert home is not None and home.url == f"https://127.0.0.1:{port}"
        assert f"Open to the home network at https://127.0.0.1:{port}." in web.get("/settings/devices").text
        context = ssl.create_default_context(cafile=str(tls.paths().ca))
        answer = httpx.get(f"{home.url}/ping", verify=context, timeout=10)
        assert answer.text == "financeproject"
        with server.socket.create_connection(("127.0.0.1", port), timeout=10) as raw, context.wrap_socket(raw, server_hostname="127.0.0.1") as tls_socket:
            assert tls_socket.version() in ("TLSv1.2", "TLSv1.3")
            name = tls_socket.cipher()[0]
            assert "GCM" in name or "CHACHA20" in name, name
        assert httpx.get(f"{home.url}/", verify=context, timeout=10).status_code == 401
        with pytest.raises(httpx.ConnectError):  # without the authority the certificate is not trusted
            httpx.get(f"{home.url}/ping", timeout=10)
        assert server.open_lan(web.app) == home  # already open
    finally:
        assert post(web, "/settings/devices/close").status_code == 303
    assert web.app.state.lan is None and not server.close_lan(web.app)
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"https://127.0.0.1:{port}/ping", verify=False, timeout=2)
    # a port in use is told plainly
    with server.socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        with pytest.raises(FinanceError, match="in use or not available"):
            server.open_lan(web.app, address="127.0.0.1", port=busy.getsockname()[1])
    assert web.app.state.lan is None


def test_opening_by_itself_only_on_the_same_network(web, monkeypatch):
    web.app.state.devices.set_password(PASSWORD)
    config.save_setting("network", "address", "192.168.1.23")
    monkeypatch.setattr(lan, "addresses", lambda: [ipaddress.ip_address("10.20.30.40")])  # a café's network
    with pytest.raises(FinanceError, match="may be on another network"):
        server.open_lan(web.app, same_network=True)
    assert web.app.state.lan is None


def test_the_command_line_sets_the_password_and_forgets_devices():
    runner = CliRunner()
    answer = runner.invoke(cli_app, ["ui", "--password"], input=f"{PASSWORD}\n{PASSWORD}\n")
    assert answer.exit_code == 0 and "Password saved." in answer.output
    assert DeviceStore().check_password(PASSWORD)
    DeviceStore().add("Phone", devices.PHONE)
    answer = runner.invoke(cli_app, ["ui", "--forget-devices"])
    assert answer.exit_code == 0 and "Removed 1 paired device." in answer.output and DeviceStore().devices == []


def test_phones_get_a_menu_an_add_button_and_icons(web):
    page = web.get("/").text
    assert 'class="menu-button"' in page and 'class="fab" href="/transactions/new"' in page and "/static/manifest.webmanifest" in page
    assert 'class="fab"' not in web.get("/transactions/new").text
    manifest = web.get("/static/manifest.webmanifest")
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    for icon in json.loads(manifest.text)["icons"]:
        assert web.get(icon["src"]).status_code == 200, icon
    assert json.loads(web.get("/static/monitor.webmanifest").text)["start_url"] == "/monitor"
    assert web.get("/static/img/icon-180.png").headers["content-type"] == "image/png"
