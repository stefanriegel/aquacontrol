from pathlib import Path

FIXTURES = Path(__file__).parent


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()
