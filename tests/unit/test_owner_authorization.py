from app.telegram.handlers_owner import OwnerHandlers


def test_owner_controls_are_private_and_allowlisted() -> None:
    handlers = OwnerHandlers.__new__(OwnerHandlers)
    handlers.owner_ids = frozenset({11})
    assert handlers._allowed(11, "private")
    assert not handlers._allowed(12, "private")
    assert not handlers._allowed(11, "group")
