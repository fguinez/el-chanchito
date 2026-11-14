"""The test session never sees the developer's real `DATABASE_URL`.

conftest neutralizes `load_dotenv` and drops `DATABASE_URL` before any test
module imports `main`, so a test that runs `run_scraper` without stubbing every
writer cannot write into the dev database. Every value here is synthetic.
"""

import os

import dotenv
import dotenv.main

_SYNTHETIC_DSN = "postgresql://user:synthetic@db.invalid:5432/must_not_load"


def test_main_binds_the_neutered_load_dotenv():
    """main's import-time `load_dotenv` is conftest's no-op, not python-dotenv's."""
    import main

    assert main.load_dotenv is dotenv.load_dotenv is dotenv.main.load_dotenv
    assert main.load_dotenv.__module__ != "dotenv.main"


def test_load_dotenv_ignores_an_explicit_env_file(tmp_path, monkeypatch):
    """Even a .env passed by path (as the login CLIs would find one) loads nothing."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(f"DATABASE_URL={_SYNTHETIC_DSN}\n", encoding="utf-8")

    from dotenv import load_dotenv

    loaded = load_dotenv(env_file, override=True)

    assert (loaded, "DATABASE_URL" in os.environ) == (False, False)
    assert dotenv.main.load_dotenv is load_dotenv
