"""Installed before pytest collection or preparation imports."""

import socket
import pytest


def denied(*args, **kwargs):
    raise AssertionError("Path A preparation forbids network and database access")


socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.getaddrinfo = denied

import asyncpg  # noqa: E402 -- network guard must be active first

asyncpg.connect = denied
asyncpg.create_pool = denied
from pydantic_settings.sources.providers.dotenv import DotEnvSettingsSource  # noqa: E402


def explicit_no_env(self):
    if self.env_file is not None:
        denied()
    return {}


DotEnvSettingsSource._read_env_files = explicit_no_env
import dotenv  # noqa: E402

dotenv.load_dotenv = denied
dotenv.dotenv_values = denied


@pytest.fixture(autouse=True)
def reject_database(request):
    assert not {"db_pool", "clean_db", "acme_session"} & set(request.fixturenames)
