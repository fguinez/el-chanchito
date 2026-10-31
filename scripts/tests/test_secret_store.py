"""Tests for scripts/secret_store.py. Every value here is synthetic."""

import os
import shlex
import shutil
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

import secret_store as ss

TRICKY = [
    "synthetic-plain",
    "with space",
    "it's quoted",
    'double "quoted"',
    "dollar $HOME and ${PATH} and $(echo nope) and `echo nope`",
    "back\\slash",
    "hash # not a comment",
    "tab\tand\nnewline",
    "carriage\rreturn",
    "x\rFINTUAL_PASSWORD=injected",
    "=leading-equals",
]


# --- file backend ---------------------------------------------------------------


def test_file_backend_round_trip(tmp_path):
    backend = ss.FileBackend(tmp_path / "local" / "secrets.env")
    assert backend.get("FINTUAL_PASSWORD") is None

    backend.set("FINTUAL_PASSWORD", "synthetic-one")
    backend.set("BUDA_API_KEY", "synthetic-two")
    backend.set("FINTUAL_PASSWORD", "synthetic-three")

    assert backend.get("FINTUAL_PASSWORD") == "synthetic-three"
    assert backend.get("BUDA_API_KEY") == "synthetic-two"
    text = backend.path.read_text()
    assert text.count("FINTUAL_PASSWORD=") == 1
    assert text.startswith("#")


@pytest.mark.parametrize("value", TRICKY)
def test_file_backend_tricky_values(tmp_path, value):
    backend = ss.FileBackend(tmp_path / "secrets.env")
    backend.set("SKIPO_API_KEY", value)

    assert ss.FileBackend(backend.path).get("SKIPO_API_KEY") == value


def test_file_backend_keeps_hand_written_lines(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text(
        "# my notes\n"
        "export BUDA_API_KEY='single quoted'\n"
        'BUDA_API_SECRET="dq \\"x\\""\n'
        "SKIPO_API_KEY=bare-value # trailing comment\n"
    )
    if os.name != "nt":
        path.chmod(0o600)
    backend = ss.FileBackend(path)

    assert backend.get("BUDA_API_KEY") == "single quoted"
    assert backend.get("BUDA_API_SECRET") == 'dq "x"'
    assert backend.get("SKIPO_API_KEY") == "bare-value"

    backend.set("BANCHILE_PASSWORD", "synthetic")
    assert "# my notes" in path.read_text()
    assert backend.get("BUDA_API_KEY") == "single quoted"


def test_carriage_return_never_starts_a_new_entry(tmp_path):
    backend = ss.FileBackend(tmp_path / "secrets.env")

    backend.set("SKIPO_API_KEY", "x\rFINTUAL_PASSWORD=injected")

    reread = ss.FileBackend(backend.path).read_all()
    assert reread == {"SKIPO_API_KEY": "x\rFINTUAL_PASSWORD=injected"}
    assert b"\r" not in backend.path.read_bytes()


def test_a_raw_carriage_return_in_a_hand_written_line_stays_in_its_value(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_bytes(b"SKIPO_API_KEY=x\rFINTUAL_PASSWORD=injected\r\nBUDA_API_KEY=y\r\n")
    if os.name != "nt":
        path.chmod(0o600)

    values = ss.FileBackend(path).read_all()

    assert values == {"SKIPO_API_KEY": "x\rFINTUAL_PASSWORD=injected", "BUDA_API_KEY": "y"}


def test_rewrites_do_not_grow_blank_lines(tmp_path):
    backend = ss.FileBackend(tmp_path / "secrets.env")
    for n in range(3):
        backend.set(f"KEY_{n}", "synthetic")

    assert "\n\n" not in backend.path.read_text()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_file_backend_creates_owner_only_file(tmp_path):
    backend = ss.FileBackend(tmp_path / "local" / "secrets.env")
    backend.set("FINTUAL_PASSWORD", "synthetic")

    assert stat.S_IMODE(backend.path.stat().st_mode) == 0o600
    # The temp file used for the atomic swap is gone.
    assert [p.name for p in backend.path.parent.iterdir()] == ["secrets.env"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_file_backend_restores_0600_on_rewrite(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text("FINTUAL_PASSWORD=synthetic\n")
    path.chmod(0o644)

    ss.FileBackend(path).set("BUDA_API_KEY", "synthetic")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_file_backend_warns_when_others_can_read(tmp_path, capsys):
    path = tmp_path / "secrets.env"
    path.write_text("FINTUAL_PASSWORD=synthetic-secret\n")
    path.chmod(0o644)

    assert ss.FileBackend(path).get("FINTUAL_PASSWORD") == "synthetic-secret"

    err = capsys.readouterr().err
    assert "chmod 600" in err
    assert "synthetic-secret" not in err


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
@pytest.mark.parametrize(
    ("mode", "label"),
    [(0o600, "(owner-only)"), (0o644, "(mode 0644, readable by others)")],
)
def test_file_backend_describes_the_actual_mode(tmp_path, mode, label):
    """The banner never calls a file owner-only that other users can read."""
    path = tmp_path / "secrets.env"
    path.write_text("FINTUAL_PASSWORD=synthetic\n")
    path.chmod(mode)

    assert ss.FileBackend(path).describe().endswith(label)


def test_file_backend_skips_permission_check_on_windows(tmp_path, capsys):
    path = tmp_path / "secrets.env"
    path.write_text("FINTUAL_PASSWORD=synthetic\n")
    if os.name != "nt":
        path.chmod(0o644)

    assert ss.FileBackend(path, is_windows=True).get("FINTUAL_PASSWORD") == "synthetic"
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("", None),
        ("   ", None),
        ("# comment", None),
        ("not a pair", None),
        ("1BAD=x", None),
        ("KEY=", ("KEY", "")),
        ("KEY = spaced ", ("KEY", "spaced")),
        ("export KEY=v", ("KEY", "v")),
        ("KEY='a # b'", ("KEY", "a # b")),
        ('KEY="a\\nb"', ("KEY", "a\nb")),
        ('KEY="a\\rb"', ("KEY", "a\rb")),
        ("KEY=a=b", ("KEY", "a=b")),
    ],
)
def test_parse_dotenv_line(line, expected):
    assert ss.parse_dotenv_line(line) == expected


# --- export ---------------------------------------------------------------------


class MemoryBackend(ss.Backend):
    name = "memory"

    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, key):
        return self.values.get(key) or None

    def set(self, key, value):
        self.values[key] = value


def _export(backend, keys=None, env=None):
    lines = []
    ss.cmd_export(backend, keys, env=env or {}, out=lines.append)
    return lines


def test_export_skips_keys_already_in_the_environment():
    backend = MemoryBackend({"BANCHILE_PASSWORD": "from-store", "BUDA_API_KEY": "from-store"})

    lines = _export(backend, env={"BANCHILE_PASSWORD": "from-env", "BUDA_API_KEY": ""})

    assert lines == ["export BUDA_API_KEY=from-store"]


def test_export_default_keys_leave_out_dashboard_password_and_load_legacy_token():
    backend = MemoryBackend(
        {"DASHBOARD_PASSWORD": "synthetic", "FINTUAL_TOKEN": "synthetic-legacy"}
    )

    assert _export(backend) == ["export FINTUAL_TOKEN=synthetic-legacy"]
    assert _export(backend, ["DASHBOARD_PASSWORD"]) == ["export DASHBOARD_PASSWORD=synthetic"]
    assert "FINTUAL_TOKEN" not in ss.SECRET_KEYS  # never prompted for


def test_export_of_named_keys_fails_when_one_has_no_value(capsys):
    backend = MemoryBackend({"BUDA_API_KEY": "synthetic"})

    lines = []
    code = ss.cmd_export(backend, ["BUDA_API_KEY", "DASHBOARD_PASSWORD"], env={}, out=lines.append)

    assert code == 1
    assert "DASHBOARD_PASSWORD" in capsys.readouterr().err
    # A key already in the environment counts as present.
    assert ss.cmd_export(backend, ["DASHBOARD_PASSWORD"], env={"DASHBOARD_PASSWORD": "x"},
                         out=lines.append) == 0


def test_export_warns_when_the_store_is_empty(capsys):
    assert _export(MemoryBackend()) == []
    assert "No secrets found in memory" in capsys.readouterr().err


def test_export_stays_quiet_when_everything_comes_from_the_environment(capsys):
    _export(MemoryBackend(), env={key: "synthetic" for key in ss.EXPORT_KEYS})

    assert capsys.readouterr().err == ""


def test_export_notes_an_automatic_fallback_to_the_file(tmp_path, capsys):
    backend = ss.FileBackend(tmp_path / "secrets.env")
    backend.set("BUDA_API_KEY", "synthetic-secret")
    backend.fallback_reason = "the Secret Service collection is locked"

    assert _export(backend) == ["export BUDA_API_KEY=synthetic-secret"]

    err = capsys.readouterr().err
    assert "the Secret Service collection is locked" in err
    assert str(backend.path) in err
    assert "synthetic-secret" not in err


def test_export_rejects_unknown_keys():
    with pytest.raises(ss.SecretStoreError):
        _export(MemoryBackend(), ["NOT_A_SECRET"])


def test_export_keeps_going_when_one_read_fails(capsys):
    class Flaky(MemoryBackend):
        def get(self, key):
            if key == "BANCHILE_PASSWORD":
                raise ss.SecretStoreError("keyring could not read BANCHILE_PASSWORD")
            return super().get(key)

    lines = _export(Flaky({"BUDA_API_KEY": "synthetic"}))

    assert lines == ["export BUDA_API_KEY=synthetic"]
    assert "BANCHILE_PASSWORD" in capsys.readouterr().err


@pytest.mark.parametrize("value", TRICKY)
def test_export_lines_are_shell_safe(value):
    (line,) = _export(MemoryBackend({"SKIPO_API_KEY": value}))

    assert shlex.split(line) == ["export", f"SKIPO_API_KEY={value}"]


@pytest.mark.skipif(shutil.which("sh") is None or os.name == "nt", reason="needs a POSIX sh")
@pytest.mark.parametrize("value", TRICKY)
def test_export_lines_survive_eval_in_sh(value):
    (line,) = _export(MemoryBackend({"SKIPO_API_KEY": value}))

    result = subprocess.run(
        ["sh", "-c", 'eval "$1"; printf %s "$SKIPO_API_KEY"', "sh", line],
        capture_output=True,
        check=True,
        env={"PATH": os.environ.get("PATH", "")},
    )
    # Bytes: text mode would turn a "\r" in the value into "\n".
    assert result.stdout.decode("utf-8") == value


# --- init / set / status --------------------------------------------------------


def test_init_skips_existing_and_stores_missing():
    backend = MemoryBackend({"BANCHILE_PASSWORD": "synthetic-existing"})
    answers = {"BUDA_API_KEY": "synthetic-new"}
    out = []

    ss.cmd_init(backend, prompt=lambda key: answers.get(key), out=out.append)

    assert backend.values == {
        "BANCHILE_PASSWORD": "synthetic-existing",
        "BUDA_API_KEY": "synthetic-new",
    }
    text = "\n".join(out)
    assert "= BANCHILE_PASSWORD already stored" in text
    assert "+ BUDA_API_KEY stored" in text
    assert "- SKIPO_API_KEY skipped" in text
    assert "synthetic" not in text


class UnreadableBackend(MemoryBackend):
    def has(self, key):
        if key == "LIDER_BCI_PASSWORD":
            raise ss.SecretStoreError(f"keyring could not read {key}: DBusException")
        return super().has(key)


def test_init_carries_on_past_a_failing_key():
    backend = UnreadableBackend()

    def prompt(key):
        if key == "SANTANDER_PASSWORD":
            raise ss.SecretStoreError(f"The two values for {key} differ; nothing stored.")
        return "synthetic-new"

    code = ss.cmd_init(backend, prompt=prompt, out=lambda _: None)

    assert code == 1
    assert set(backend.values) == set(ss.SECRET_KEYS) - {"LIDER_BCI_PASSWORD", "SANTANDER_PASSWORD"}


def test_status_lists_every_key_past_an_unreadable_one():
    out = []

    code = ss.cmd_status(UnreadableBackend(), env={}, out=out.append)

    assert code == 1
    assert "? LIDER_BCI_PASSWORD (unreadable)" in out
    assert f"x {ss.SECRET_KEYS[-1]} (missing)" in out


@pytest.mark.parametrize("key", ["bad-name", "NEW_BANK_PASSWORD"])
def test_set_only_takes_a_secret_export_can_load(key):
    with pytest.raises(ss.SecretStoreError):
        ss.cmd_set(MemoryBackend(), key, prompt=lambda key: "x", out=lambda _: None)


def test_set_overwrites():
    backend = MemoryBackend({"FINTUAL_PASSWORD": "synthetic-old"})

    code = ss.cmd_set(backend, "FINTUAL_PASSWORD", prompt=lambda key: "synthetic-new", out=lambda _: None)

    assert code == 0
    assert backend.values["FINTUAL_PASSWORD"] == "synthetic-new"


def test_status_never_prints_values():
    backend = MemoryBackend({"BANCHILE_PASSWORD": "synthetic-secret"})
    out = []

    ss.cmd_status(backend, env={"BUDA_API_KEY": "synthetic-env"}, out=out.append)

    text = "\n".join(out)
    assert "+ BANCHILE_PASSWORD" in text
    assert "x BUDA_API_KEY (missing; set in the environment)" in text
    assert "x SKIPO_API_KEY (missing)" in text
    assert "synthetic" not in text


# --- backend selection ----------------------------------------------------------


class FakeKeyringBackend:
    __module__ = "tests.fake_keyring"


class FailKeyringBackend:
    __module__ = "keyring.backends.fail"


class FakeSecretService:
    __module__ = "keyring.backends.SecretService"


def fake_keyring(backend_cls=FakeKeyringBackend):
    store = {}
    return SimpleNamespace(
        store=store,
        get_keyring=lambda: backend_cls(),
        get_password=lambda service, user: store.get((service, user)),
        set_password=lambda service, user, value: store.__setitem__((service, user), value),
    )


def _select(env=None, platform="linux", security=False, keyring=None, probe=None):
    env = {"USER": "synthetic-user", **(env or {})}
    return ss.select_backend(
        env=env,
        platform=platform,
        which=lambda name: "/usr/bin/security" if security and name == "security" else None,
        keyring_loader=lambda: keyring,
        probe=probe or (lambda backend: pytest.fail("only SecretService is probed")),
    )


def test_an_unlocked_secret_service_is_used():
    backend = _select(keyring=fake_keyring(FakeSecretService), probe=lambda b: True)

    assert isinstance(backend, ss.KeyringBackend)


def _raise(backend):
    raise RuntimeError("no D-Bus session")


@pytest.mark.parametrize(
    ("probe", "reason"),
    [
        (lambda backend: False, "the Secret Service collection is locked"),
        (_raise, "the Secret Service could not be checked"),
    ],
)
def test_a_locked_or_unreachable_secret_service_falls_back_to_the_file(probe, reason):
    backend = _select(keyring=fake_keyring(FakeSecretService), probe=probe)

    assert isinstance(backend, ss.FileBackend)
    assert backend.fallback_reason == reason
    assert reason in ss.backend_banner(backend)


def test_a_chainer_with_a_locked_secret_service_is_not_used():
    class ChainerBackend:
        __module__ = "keyring.backends.chainer"
        backends = [FakeSecretService()]

    assert ss.keyring_unusable_reason(fake_keyring(ChainerBackend), probe=lambda b: False)
    assert ss.keyring_unusable_reason(fake_keyring(ChainerBackend), probe=lambda b: True) is None


def test_forcing_the_file_backend_is_not_a_fallback():
    backend = _select(env={"CHANCHITO_SECRETS_BACKEND": "file"}, keyring=fake_keyring())

    assert backend.fallback_reason is None


def test_auto_prefers_a_usable_keyring():
    backend = _select(keyring=fake_keyring())

    assert isinstance(backend, ss.KeyringBackend)
    assert "fake_keyring" in backend.describe()


def test_auto_falls_back_to_the_file_when_keyring_only_has_its_fail_backend(tmp_path):
    backend = _select(
        env={"CHANCHITO_SECRETS_FILE": str(tmp_path / "s.env")},
        keyring=fake_keyring(FailKeyringBackend),
    )

    assert isinstance(backend, ss.FileBackend)
    assert backend.path == tmp_path / "s.env"
    assert backend.fallback_reason == "keyring found no system keyring"


def test_auto_falls_back_to_the_default_file_without_keyring():
    backend = _select(keyring=None)

    assert isinstance(backend, ss.FileBackend)
    assert backend.path == ss.DEFAULT_SECRETS_FILE
    assert backend.fallback_reason == "the keyring package is not installed"


def test_auto_uses_the_keychain_on_macos_even_with_keyring():
    backend = _select(platform="darwin", security=True, keyring=fake_keyring())

    assert isinstance(backend, ss.KeychainBackend)
    assert backend.user == "synthetic-user"


def test_macos_without_the_security_cli_uses_keyring():
    assert isinstance(_select(platform="darwin", keyring=fake_keyring()), ss.KeyringBackend)


@pytest.mark.parametrize(
    ("forced", "expected"),
    [("file", ss.FileBackend), ("FILE", ss.FileBackend), ("keyring", ss.KeyringBackend)],
)
def test_forced_backend(forced, expected):
    backend = _select(env={"CHANCHITO_SECRETS_BACKEND": forced}, platform="darwin",
                      security=True, keyring=fake_keyring())

    assert isinstance(backend, expected)


@pytest.mark.parametrize(
    ("forced", "kwargs"),
    [
        ("keyring", {"keyring": fake_keyring(FailKeyringBackend)}),
        ("keyring", {"keyring": None}),
        ("keychain", {"platform": "linux"}),
        ("vault", {}),
    ],
)
def test_forced_backend_that_cannot_work_is_an_error(forced, kwargs):
    with pytest.raises(ss.SecretStoreError):
        _select(env={"CHANCHITO_SECRETS_BACKEND": forced}, **kwargs)


def test_keyring_backend_uses_the_keychain_item_names():
    module = fake_keyring()
    backend = ss.KeyringBackend(module, "synthetic-user")

    backend.set("FINTUAL_PASSWORD", "synthetic")

    assert module.store == {("chanchito.FINTUAL_PASSWORD", "synthetic-user"): "synthetic"}
    assert backend.get("FINTUAL_PASSWORD") == "synthetic"
    assert backend.has("FINTUAL_PASSWORD")
    assert not backend.has("BUDA_API_KEY")


def test_keyring_errors_never_include_the_value():
    def boom(service, user, value):
        raise RuntimeError(f"cannot store {value}")

    module = SimpleNamespace(set_password=boom)
    with pytest.raises(ss.SecretStoreError) as info:
        ss.KeyringBackend(module, "synthetic-user").set("BUDA_API_KEY", "synthetic-secret")

    assert "synthetic-secret" not in str(info.value)


def test_current_user_falls_back_to_getpass(monkeypatch):
    monkeypatch.setattr(ss.getpass, "getuser", lambda: "synthetic-login")

    assert ss.current_user({}) == "synthetic-login"
    assert ss.current_user({"USER": "synthetic-user"}) == "synthetic-user"


# --- keychain backend (security CLI, faked) ---------------------------------------


class FakeSecurity:
    def __init__(self, stored):
        self.stored = stored
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        service = args[args.index("-s") + 1]
        if args[1] == "find-generic-password":
            if service not in self.stored:
                return SimpleNamespace(returncode=44, stdout="")
            stdout = self.stored[service] + "\n" if "-w" in args else ""
            return SimpleNamespace(returncode=0, stdout=stdout)
        return SimpleNamespace(returncode=0, stdout="")


def test_keychain_backend_matches_the_historical_security_calls():
    fake = FakeSecurity({"chanchito.BANCHILE_PASSWORD": "synthetic"})
    backend = ss.KeychainBackend("synthetic-user", runner=fake)

    assert backend.get("BANCHILE_PASSWORD") == "synthetic"
    assert backend.get("BUDA_API_KEY") is None
    assert backend.has("BANCHILE_PASSWORD")
    assert fake.calls[0] == [
        "security", "find-generic-password", "-a", "synthetic-user",
        "-s", "chanchito.BANCHILE_PASSWORD", "-w",
    ]
    # Existence checks never read the password data (no access prompt).
    assert "-w" not in fake.calls[-1]

    backend.store_interactive("BUDA_API_KEY", prompt=lambda key: pytest.fail("no prompt"))
    assert fake.calls[-1] == [
        "security", "add-generic-password", "-U", "-a", "synthetic-user",
        "-s", "chanchito.BUDA_API_KEY", "-w",
    ]


def test_main_reports_store_errors_without_a_traceback(monkeypatch, capsys):
    monkeypatch.setenv("CHANCHITO_SECRETS_BACKEND", "vault")

    assert ss.main(["status"]) == 1
    assert "vault" in capsys.readouterr().err


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, EOFError])
def test_main_exits_quietly_when_a_prompt_is_aborted(tmp_path, monkeypatch, capsys, interrupt):
    """Ctrl-C or Ctrl-D at the hidden prompt aborts without a traceback."""
    monkeypatch.setattr(ss, "select_backend", lambda: ss.FileBackend(tmp_path / "secrets.env"))

    def abort(_prompt):
        raise interrupt

    monkeypatch.setattr(ss.getpass, "getpass", abort)

    assert ss.main(["set", "FINTUAL_PASSWORD"]) == 130
    assert "Traceback" not in capsys.readouterr().err


def test_main_rejects_bad_usage_before_touching_a_store(monkeypatch, capsys):
    monkeypatch.setattr(ss, "select_backend", lambda: pytest.fail("no backend needed"))

    assert ss.main(["set"]) == 2
    assert ss.main(["status", "extra"]) == 2
    assert ss.main(["nope"]) == 2
    assert ss.main([]) == 2
    assert capsys.readouterr().out == ""


@pytest.mark.skipif(sys.platform == "win32", reason="the CLI is driven from POSIX sh")
def test_cli_file_backend_end_to_end(tmp_path):
    path = tmp_path / "secrets.env"
    ss.FileBackend(path).set("BUDA_API_KEY", "synthetic $value")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "USER": "synthetic-user",
        "CHANCHITO_SECRETS_BACKEND": "file",
        "CHANCHITO_SECRETS_FILE": str(path),
    }

    status = subprocess.run(
        [sys.executable, ss.__file__, "status"], env=env, capture_output=True, text=True, check=True
    )
    export = subprocess.run(
        [sys.executable, ss.__file__, "export"], env=env, capture_output=True, text=True, check=True
    )

    assert "+ BUDA_API_KEY" in status.stdout
    assert "synthetic" not in status.stdout
    assert export.stdout == "export BUDA_API_KEY='synthetic $value'\n"
    named = subprocess.run(
        [sys.executable, ss.__file__, "export", "DASHBOARD_PASSWORD"],
        env=env, capture_output=True, text=True,
    )
    assert named.returncode == 1
    assert named.stdout == ""


def test_cli_export_writes_utf8_with_lf(tmp_path):
    path = tmp_path / "secrets.env"
    ss.FileBackend(path).set("BUDA_API_KEY", "synthetic-ñandú")
    env = {
        **os.environ,
        "PYTHONIOENCODING": "cp1252",
        "CHANCHITO_SECRETS_BACKEND": "file",
        "CHANCHITO_SECRETS_FILE": str(path),
    }

    result = subprocess.run(
        [sys.executable, ss.__file__, "export", "BUDA_API_KEY"],
        env=env, capture_output=True, check=True,
    )

    assert result.stdout == "export BUDA_API_KEY='synthetic-ñandú'\n".encode("utf-8")
