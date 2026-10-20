"""The certificates of the interface's home-network address (HTTPS).

A certificate authority of this computer's own signs the server's certificate. A phone that installs the
authority once trusts the server without a warning, also after the server's certificate is renewed; a phone
that does not install it gets the browser's warning on its first visit.

- **The authority**: an EC P-256 key, valid for ten years, allowed to sign server certificates only
  (``pathLenConstraint`` 0). It is name-constrained (RFC 5280, 4.2.1.10): it can vouch only for addresses in the
  private ranges (10/8, 172.16/12, 192.168/16, 169.254/16, 127/8) and for names under ``.local`` and this
  computer's own name, so a phone that trusts it trusts nothing on the internet.
- **The server's certificate**: an EC P-256 key, valid for 397 days (within every browser's limit), for this
  computer's name, its ``.local`` name and its home-network addresses. It is made again when it is missing,
  expires within 30 days, or does not cover the current names and addresses.

The files are in ``tls/`` of the configuration directory; the keys are readable only by the user.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from financeproject import config
from financeproject.web.lan import PRIVATE_NETWORKS

CA_DAYS = 10 * 365
SERVER_DAYS = 397
RENEW_DAYS = 30
ORGANIZATION = "financeproject"


@dataclass(frozen=True)
class Paths:
    ca: Path
    ca_key: Path
    server: Path
    server_key: Path


def tls_dir() -> Path:
    return config.config_dir() / "tls"


def paths(folder: Path | None = None) -> Paths:
    folder = folder or tls_dir()
    return Paths(folder / "ca.pem", folder / "ca.key", folder / "server.pem", folder / "server.key")


def _write(path: Path, data: bytes, secret: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o600 if secret else 0o644
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(path, mode)


def _key_bytes(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def _new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _names(hostname: str) -> list[str]:
    return [hostname, f"{hostname}.local"] if hostname else []


def make_authority(hostname: str, now: datetime | None = None) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    now = now or datetime.now(UTC)
    key = _new_key()
    subject = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, ORGANIZATION),
        x509.NameAttribute(NameOID.COMMON_NAME, f"financeproject on {hostname or 'this computer'}"),
    ])
    permitted: list[x509.GeneralName] = [x509.DNSName("local")]
    if hostname:
        permitted.append(x509.DNSName(hostname))
    permitted += [x509.IPAddress(network) for network in PRIVATE_NETWORKS]
    public = key.public_key()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(public)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False, content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.NameConstraints(permitted_subtrees=permitted, excluded_subtrees=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public), critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert, key


def make_server(
    authority: x509.Certificate,
    authority_key: ec.EllipticCurvePrivateKey,
    hostname: str,
    addresses: list[ipaddress.IPv4Address],
    now: datetime | None = None,
) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    now = now or datetime.now(UTC)
    key = _new_key()
    names: list[x509.GeneralName] = [x509.DNSName(n) for n in _names(hostname)]
    names += [x509.IPAddress(ip) for ip in addresses]
    common = f"{hostname}.local" if hostname else str(addresses[0])
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, ORGANIZATION), x509.NameAttribute(NameOID.COMMON_NAME, common)]))
        .issuer_name(authority.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=SERVER_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(authority_key.public_key()), critical=False)
        .sign(authority_key, hashes.SHA256())
    )
    return cert, key


def _covers(cert: x509.Certificate, hostname: str, addresses: list[ipaddress.IPv4Address], now: datetime) -> bool:
    if cert.not_valid_after_utc - now < timedelta(days=RENEW_DAYS):
        return False
    try:
        sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return False
    return set(_names(hostname)) <= set(sans.get_values_for_type(x509.DNSName)) and set(addresses) <= set(
        sans.get_values_for_type(x509.IPAddress)
    )


def _permits(authority: x509.Certificate, hostname: str) -> bool:
    """True if the authority's name constraints allow ``hostname`` (a new computer name needs a new authority)."""
    if not hostname:
        return True
    try:
        constraints = authority.extensions.get_extension_for_class(x509.NameConstraints).value
    except x509.ExtensionNotFound:
        return False
    return hostname in [n.value for n in constraints.permitted_subtrees or [] if isinstance(n, x509.DNSName)]


def _load(path: Path) -> x509.Certificate | None:
    try:
        return x509.load_pem_x509_certificate(path.read_bytes())
    except (OSError, ValueError):
        return None


def ensure(hostname: str, addresses: list[ipaddress.IPv4Address], folder: Path | None = None) -> Paths:
    """The certificates for ``hostname`` and ``addresses``, made or renewed when needed."""
    if not addresses:
        raise ValueError("A server certificate needs at least one address.")
    where = paths(folder)
    now = datetime.now(UTC)
    authority = _load(where.ca)
    authority_key = None
    usable = authority is not None and authority.not_valid_after_utc - now > timedelta(days=SERVER_DAYS)
    if usable and where.ca_key.exists() and _permits(authority, hostname):
        authority_key = serialization.load_pem_private_key(where.ca_key.read_bytes(), password=None)
    else:
        authority = None
    if authority is None or not isinstance(authority_key, ec.EllipticCurvePrivateKey):
        authority, authority_key = make_authority(hostname, now)
        _write(where.ca_key, _key_bytes(authority_key), secret=True)
        _write(where.ca, authority.public_bytes(serialization.Encoding.PEM), secret=False)
        where.server.unlink(missing_ok=True)
    server = _load(where.server)
    if server is None or server.issuer != authority.subject or not where.server_key.exists() or not _covers(server, hostname, addresses, now):
        server, server_key = make_server(authority, authority_key, hostname, addresses, now)
        _write(where.server_key, _key_bytes(server_key), secret=True)
        _write(where.server, server.public_bytes(serialization.Encoding.PEM), secret=False)
    return where


def authority_der(folder: Path | None = None) -> bytes | None:
    """The authority's certificate (DER), for a phone to install; ``None`` before it exists."""
    cert = _load(paths(folder).ca)
    return cert.public_bytes(serialization.Encoding.DER) if cert is not None else None


def fingerprint(folder: Path | None = None) -> str | None:
    """The authority's SHA-256 fingerprint, ``AB:CD:…``, to compare with what a phone shows."""
    cert = _load(paths(folder).ca)
    if cert is None:
        return None
    return ":".join(f"{b:02X}" for b in cert.fingerprint(hashes.SHA256()))


def server_expiry(folder: Path | None = None) -> datetime | None:
    cert = _load(paths(folder).server)
    return cert.not_valid_after_utc if cert is not None else None
