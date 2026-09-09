from collections.abc import Iterator

import pytest

from commander.domain import Alert, Incident
from commander.storage import Base, Repository, make_engine


@pytest.fixture
def repository() -> Iterator[Repository]:
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield Repository(engine)
    engine.dispose()


@pytest.fixture
def incident(repository: Repository) -> Incident:
    return repository.create_incident(Alert(title="Checkout error", service="demo-api"))
