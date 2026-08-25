from .auth import login, register


def register_route(email: str, password_hash: str) -> dict[str, str]:
    return register(email, password_hash)


def login_route(email: str, password_hash: str, user: dict[str, str]) -> bool:
    return login(email, password_hash, user)
