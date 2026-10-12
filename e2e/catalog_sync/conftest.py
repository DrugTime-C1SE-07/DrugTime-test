"""Fixtures are opt-in; existing unit tests never start infra."""

import signal

import pytest

from .harness import Case, Infrastructure


@pytest.fixture(scope="session")
def catalog_infra():
    def expire(*_):
        raise TimeoutError("Global 1200 second catalog suite deadline exceeded")

    previous = signal.signal(signal.SIGALRM, expire)
    signal.alarm(1200)
    infra = None
    try:
        infra = Infrastructure()
        yield infra.start()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        if infra:
            infra.close()


@pytest.fixture
def catalog_case(catalog_infra, request):
    case = Case(catalog_infra, request.node.name)
    try:
        yield case.start()
    finally:
        try:
            case.snapshot()  # Always collect DB/ledger before teardown.
        finally:
            case.close()
