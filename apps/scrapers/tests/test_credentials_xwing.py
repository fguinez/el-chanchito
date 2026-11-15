"""Credential scheme v2 (X-Wing: ML-KEM-768 + X25519, HPKE KEM 0x647a): its
key file, its sizes, the published test vectors, the dashboard's v2 fixture,
and the move from a v1 key to a v2 one by rotation.

Every value is synthetic, and every key is generated per test or is a
published test-vector or fixture key, which protects nothing.
"""

import base64
import dataclasses
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest
from credential_manager import rotate_key_file
from cryptography.hazmat.bindings._rust import openssl as rust_openssl
from cryptography.hazmat.primitives import hpke

from scrapers.credentials import (
    V1,
    V2,
    CredentialKey,
    CredentialKeyError,
    CredentialOpenError,
    KeyFile,
    SealedCredential,
    binding_digest,
    generate_key,
    load_key,
    load_or_create_key,
    open_rows,
    reseal_rows,
)
from tests.fakes_credentials import FakeRing, make_v1_key

FIXTURES = Path(__file__).parent / "fixtures" / "credentials"
VECTORS = FIXTURES / "xwing_vectors.json"
TS_FIXTURE_V2 = FIXTURES / "ts_sealed_v2.json"
SECRET = "synthetic-password-1234"
HOST, USER = "imap.gmail.com", "user@example.com"
XWING_PKCS8_PREFIX = bytes.fromhex("3034020100300d060b2b0601040183e62d81c87a0420")


def _row(key: CredentialKey, slug: str, field: str, value: str, binding=None) -> SealedCredential:
    return SealedCredential(
        slug, field, key.scheme, key.key_id, key.seal(slug, field, value, binding)
    )


def _write_owner_only(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, data)
    os.close(fd)


def _pem(der: bytes) -> bytes:
    body = base64.b64encode(der)
    lines = b"\n".join(body[i : i + 64] for i in range(0, len(body), 64))
    return b"-----BEGIN PRIVATE KEY-----\n" + lines + b"\n-----END PRIVATE KEY-----\n"


# --- Keys and sizes -------------------------------------------------------------


def test_new_keys_are_x_wing():
    key = generate_key()
    assert key.kind is V2
    assert key.scheme == "hpke-mlkem768x25519-sha256-aes256gcm-v2"
    assert key.algorithm == "HPKE-Base(MLKEM768-X25519, HKDF-SHA256, AES-256-GCM)"
    assert len(key.public_raw) == 1216
    assert key.key_id == hashlib.sha256(key.public_raw).hexdigest()
    assert key.seed is not None and key.seed.hex() not in repr(key)


def test_v2_ciphertext_is_encapsulated_key_then_aes_gcm():
    key = generate_key()
    sealed = key.seal("banchile", "password", SECRET)
    assert len(sealed) == 1120 + len(SECRET) + 16
    assert key.open("banchile", "password", key.key_id, sealed) == SECRET


def test_the_scheme_is_part_of_the_sealing_context():
    """The same X25519 key under a kind that differs only in its scheme
    string opens nothing the other sealed, either way."""
    key = make_v1_key()
    relabelled = dataclasses.replace(key, kind=dataclasses.replace(V1, scheme=V2.scheme))
    assert relabelled.key_id == key.key_id
    by_key = key.seal("banchile", "password", SECRET)
    by_relabelled = relabelled.seal("banchile", "password", SECRET)
    assert relabelled.open("banchile", "password", key.key_id, by_relabelled) == SECRET
    with pytest.raises(CredentialOpenError):
        relabelled.open("banchile", "password", key.key_id, by_key)
    with pytest.raises(CredentialOpenError):
        key.open("banchile", "password", key.key_id, by_relabelled)


def test_a_v2_ciphertext_does_not_open_with_a_v1_key():
    v1, v2 = make_v1_key(), generate_key()
    with pytest.raises(CredentialOpenError):
        v1.open("banchile", "password", v1.key_id, v2.seal("banchile", "password", SECRET))


def test_from_seed_rejects_a_wrong_length():
    with pytest.raises(CredentialKeyError):
        CredentialKey.from_seed(b"\x00" * 31)


# --- Key file -------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_v2_key_file_is_owner_only_pkcs8_with_the_seed(tmp_path):
    path = tmp_path / "credentials_key"
    key = load_or_create_key(path)
    assert key.kind is V2
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    pem = path.read_bytes()
    assert pem.startswith(b"-----BEGIN PRIVATE KEY-----\n")
    body = b"".join(pem.splitlines()[1:-1])
    assert base64.b64decode(body) == XWING_PKCS8_PREFIX + key.seed
    reloaded = load_key(path)
    assert (reloaded.kind, reloaded.key_id) == (V2, key.key_id)
    sealed = key.seal("banchile", "password", SECRET)
    assert reloaded.open("banchile", "password", key.key_id, sealed) == SECRET


def test_a_v1_key_file_still_loads_and_opens_its_rows(tmp_path):
    path = tmp_path / "credentials_key"
    v1 = make_v1_key()
    _write_owner_only(path, v1.private_pem())
    loaded = load_key(path)
    assert (loaded.kind, loaded.key_id) == (V1, v1.key_id)
    assert len(loaded.public_raw) == 32
    rows = [_row(v1, "banchile", "password", SECRET), _row(v1, "banchile", "rut", "11.111.111-1")]
    opened = open_rows(loaded, rows)
    assert opened.values == {"BANCHILE_PASSWORD": SECRET, "BANCHILE_RUT": "11.111.111-1"}
    assert opened.needs_reentry == frozenset()


@pytest.mark.parametrize(
    "der",
    [
        XWING_PKCS8_PREFIX + b"\x01" * 31,
        XWING_PKCS8_PREFIX + b"\x01" * 33,
        XWING_PKCS8_PREFIX.replace(b"\x02\x01\x00", b"\x02\x01\x01") + b"\x01" * 32,
        XWING_PKCS8_PREFIX[:-2] + b"\x04\x22\x04\x20" + b"\x01" * 32,
    ],
    ids=["short-seed", "long-seed", "version-1", "nested-octet-string"],
)
def test_a_malformed_x_wing_key_file_is_refused(tmp_path, der):
    path = tmp_path / "credentials_key"
    _write_owner_only(path, _pem(der))
    with pytest.raises(CredentialKeyError):
        load_key(path)


def test_trailing_data_after_the_pem_is_refused(tmp_path):
    path = tmp_path / "credentials_key"
    _write_owner_only(path, generate_key().private_pem() + b"extra\n")
    with pytest.raises(CredentialKeyError):
        load_key(path)


# --- Rows, re-seal and rotation -------------------------------------------------


def test_a_v2_key_flags_v1_rows_for_reentry():
    v1, v2 = make_v1_key(), generate_key()
    opened = open_rows(v2, [_row(v1, "banchile", "password", SECRET)])
    assert opened.values == {}
    assert opened.needs_reentry == {"BANCHILE_PASSWORD"}


def test_a_row_under_another_scheme_is_not_opened_even_with_the_key_id():
    key = generate_key()
    row = _row(key, "banchile", "password", SECRET)
    relabeled = SealedCredential(
        row.institution_slug, row.field_key, V1.scheme, key.key_id, row.sealed
    )
    assert open_rows(key, [relabeled]).needs_reentry == {"BANCHILE_PASSWORD"}


def test_reseal_from_v1_to_v2_moves_every_row_to_the_v2_scheme():
    old, new = make_v1_key(), generate_key()
    rows = [
        _row(old, "email", "imap_host", HOST),
        _row(old, "email", "imap_user", USER),
        _row(old, "email", "imap_password", SECRET, binding_digest([HOST, USER])),
    ]
    resealed, skipped = reseal_rows(old, new, rows)
    assert skipped == []
    assert {r.scheme for r in resealed} == {V2.scheme}
    moved = [
        SealedCredential(r.institution_slug, r.field_key, r.scheme, new.key_id, r.sealed)
        for r in resealed
    ]
    assert open_rows(new, moved).values == {
        "EMAIL_IMAP_HOST": HOST,
        "EMAIL_IMAP_USER": USER,
        "EMAIL_IMAP_PASSWORD": SECRET,
    }


@pytest.mark.skipif(os.name == "nt", reason="key rotation needs flock; native Windows refuses it")
def test_rotating_a_v1_key_file_switches_the_key_and_its_rows_to_v2(tmp_path):
    """An install from before v2 keeps its v1 key until the next rotation,
    which writes a v2 key file and stores every row under the v2 scheme."""
    ring = FakeRing()
    key_file = KeyFile(tmp_path / "credentials_key")
    old = make_v1_key()
    key_file.replace(old)
    ring.activate(old)
    ring.rows = [_row(old, "skipo", "api_key", SECRET), _row(old, "buda", "api_key", SECRET)]
    result = rotate_key_file(key_file, ring)
    assert (result.resealed, result.skipped) == (2, 0)
    assert result.new_key.kind is V2
    assert ring.active.key_id == result.new_key.key_id
    assert {(r.key_id, r.scheme) for r in ring.rows} == {(result.new_key.key_id, V2.scheme)}
    on_disk = load_key(key_file.path)
    assert (on_disk.kind, on_disk.key_id) == (V2, result.new_key.key_id)
    assert open_rows(on_disk, ring.rows).values == {
        "SKIPO_API_KEY": SECRET,
        "BUDA_API_KEY": SECRET,
    }


# --- Published test vectors -----------------------------------------------------

_KDFS = {1: hpke.KDF.HKDF_SHA256, 17: hpke.KDF.SHAKE256}
_AEADS = {1: hpke.AEAD.AES_128_GCM, 2: hpke.AEAD.AES_256_GCM, 3: hpke.AEAD.CHACHA20_POLY1305}


def _vectors():
    return json.loads(VECTORS.read_text(encoding="utf-8"))


@pytest.mark.parametrize("index", [0, 1])
def test_hpke_pq_vectors_derive_the_key_and_open(index):
    """draft-ietf-hpke-pq, KEM 0x647a: the key built from skRm (our seed
    expansion) has the vector's public key and opens its first encryption,
    with its info and aad, through the same cryptography HPKE code."""
    vector = _vectors()["hpke_pq"][index]
    assert (vector["mode"], vector["kem_id"]) == (0, 0x647A)
    key = CredentialKey.from_seed(bytes.fromhex(vector["skRm"]))
    assert key.public_raw == bytes.fromhex(vector["pkRm"])
    suite = hpke.Suite(hpke.KEM.MLKEM768_X25519, _KDFS[vector["kdf_id"]], _AEADS[vector["aead_id"]])
    first = vector["encryptions"][0]
    sealed = bytes.fromhex(vector["enc"]) + bytes.fromhex(first["ct"])
    info, aad = bytes.fromhex(vector["info"]), bytes.fromhex(first["aad"])
    plaintext = rust_openssl.hpke._decrypt_with_aad(
        suite, sealed, key.private_key, info=info, aad=aad
    )
    assert plaintext == bytes.fromhex(first["pt"])
    with pytest.raises(Exception):  # noqa: B017 (cryptography raises its own error type)
        rust_openssl.hpke._decrypt_with_aad(suite, sealed, key.private_key, info=b"x", aad=aad)


@pytest.mark.parametrize("index", [0, 1, 2])
def test_x_wing_vectors_derive_the_public_key(index):
    vector = _vectors()["xwing"][index]
    key = CredentialKey.from_seed(bytes.fromhex(vector["sk"]))
    assert key.public_raw == bytes.fromhex(vector["pk"])


def test_vectors_name_their_source():
    note = _vectors()["note"]
    assert "github.com/hpkewg/hpke-pq" in note
    assert "11b5b9541e9976fc9ce25902011d20dacc089066" in note
    assert "github.com/dconnolly/draft-connolly-cfrg-xwing-kem" in note
    assert "5cb311dc7c7761c2b82d2f4a0037da2c2c7af8f3" in note


# --- Dashboard fixture ----------------------------------------------------------


def test_dashboard_v2_fixture_opens():
    """A ciphertext sealed by the dashboard's TypeScript code to a throwaway
    X-Wing key opens here, and only in its own context. Regenerate with
    `WRITE_CREDENTIALS_FIXTURE=1 corepack pnpm --filter @chanchito/web test
    credentials-seal`."""
    fixture = json.loads(TS_FIXTURE_V2.read_text(encoding="utf-8"))
    key = CredentialKey.from_seed(bytes.fromhex(fixture["recipient_seed_hex"]))
    assert key.key_id == fixture["recipient_fingerprint"]
    assert fixture["scheme"] == V2.scheme == key.scheme
    assert fixture["note"].startswith("Synthetic")
    assert any("binding_values" in entry for entry in fixture["entries"])
    for entry in fixture["entries"]:
        sealed = base64.b64decode(entry["sealed"])
        binding = binding_digest(entry["binding_values"]) if "binding_values" in entry else None
        assert (
            key.open(entry["slug"], entry["field"], key.key_id, sealed, binding) == entry["value"]
        )
        with pytest.raises(CredentialOpenError):
            key.open(entry["slug"], "other_field", key.key_id, sealed, binding)
        if binding is not None:
            with pytest.raises(CredentialOpenError):
                key.open(entry["slug"], entry["field"], key.key_id, sealed)
