from app.auth import login, register


def test_register_then_login() -> None:
    user = register("user@example.test", "hash")
    assert login("user@example.test", "hash", user)
