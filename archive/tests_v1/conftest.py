import pytest

from app import config as app_config
from app.core.principal import load_principal
from app.data.repository import Repository


@pytest.fixture(scope="session")
def staff():
    return load_principal("agent_rohit")


@pytest.fixture(scope="session")
def manager():
    return load_principal("mgr_priya")


@pytest.fixture(scope="session")
def repo(staff):
    return Repository(staff)


def pytest_configure(config):
    config.addinivalue_line("markers", "llm: requires a live GROQ_API_KEY")


def pytest_collection_modifyitems(config, items):
    # NOTE: the parameter must literally be named `config` -- pytest matches
    # hook arguments by name against the hookspec.
    if app_config.GROQ_API_KEY:
        return
    skip = pytest.mark.skip(reason="no GROQ_API_KEY configured")
    for item in items:
        if "llm" in item.keywords:
            item.add_marker(skip)
