"""Minimal authentication domain used by the durable-loop fixture."""


def register(email: str, password_hash: str) -> dict[str, str]:
    return {"email": email, "password_hash": password_hash}


def login(email: str, password_hash: str, user: dict[str, str]) -> bool:
    return user.get("email") == email and user.get("password_hash") == password_hash
