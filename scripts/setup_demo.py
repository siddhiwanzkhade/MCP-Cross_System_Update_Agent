"""Create the demo workspace and a fresh database.

The workspace has one shared library, pricekit, in three versions:
    1.0  the version every service uses today
    1.1  a safe update (adds a function)
    2.0  a breaking update: format_price() now requires a currency

And three services, each its own git repository, all on pricekit 1.0:
    checkout  passes a currency       -> works on 1.0, 1.1 and 2.0
    billing   leaves the currency out -> its tests FAIL on 2.0
    reports   only uses round_price   -> works on every version

Run once (and again any time you want a clean start):
    python scripts/setup_demo.py
"""

import shutil
import subprocess
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.db import db_path, get_connection, init_db  # noqa: E402
from shared.workspace import GIT_IDENTITY, workspace_path  # noqa: E402

LIBRARIES = {
    ("pricekit", "1.0"): '''\
"""pricekit 1.0: money formatting helpers."""


def format_price(amount, currency="USD"):
    return f"{amount:,.2f} {currency}"


def round_price(amount):
    return round(amount, 2)
''',
    ("pricekit", "1.1"): '''\
"""pricekit 1.1: adds parse_price. Nothing else changes."""


def format_price(amount, currency="USD"):
    return f"{amount:,.2f} {currency}"


def round_price(amount):
    return round(amount, 2)


def parse_price(text):
    return float(text.split()[0].replace(",", ""))
''',
    ("pricekit", "2.0"): '''\
"""pricekit 2.0: BREAKING. format_price() now requires a currency."""


def format_price(amount, currency):
    return f"{amount:,.2f} {currency}"


def round_price(amount):
    return round(amount, 2)


def parse_price(text):
    return float(text.split()[0].replace(",", ""))
''',
}

SERVICES = {
    "checkout": {
        "app.py": '''\
from pricekit import format_price


def receipt_line(item, amount):
    return f"{item}: {format_price(amount, 'USD')}"
''',
        "tests/test_app.py": '''\
from app import receipt_line


def test_receipt_line():
    assert receipt_line("Book", 12.5) == "Book: 12.50 USD"
''',
    },
    "billing": {
        "app.py": '''\
from pricekit import format_price


def invoice_total(amounts):
    return "Total: " + format_price(sum(amounts))
''',
        "tests/test_app.py": '''\
from app import invoice_total


def test_invoice_total():
    assert invoice_total([1000, 250]) == "Total: 1,250.00 USD"
''',
    },
    "reports": {
        "app.py": '''\
from pricekit import round_price


def average(amounts):
    return round_price(sum(amounts) / len(amounts))
''',
        "tests/test_app.py": '''\
from app import average


def test_average():
    assert average([1, 2, 2]) == 1.67
''',
    },
}


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", *GIT_IDENTITY, "-C", str(path), *args], check=True, capture_output=True)


def create_demo(root: Path) -> None:
    """Write the libraries and services into root (which must not exist yet)."""
    for (package, version), source in LIBRARIES.items():
        folder = root / "libraries" / package / version / package
        folder.mkdir(parents=True)
        (folder / "__init__.py").write_text(source)

    for service, files in SERVICES.items():
        path = root / "services" / service
        files = {**files, "requirements.txt": "pricekit==1.0\n",
                 "pytest.ini": "[pytest]\n",  # its own config, so pytest never uses the project's
                 ".gitignore": "__pycache__/\n.pytest_cache/\n"}
        for name, content in files.items():
            (path / name).parent.mkdir(parents=True, exist_ok=True)
            (path / name).write_text(content)
        _git(path, "init", "-q", "-b", "main")
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", "Initial version")


def main() -> None:
    root = workspace_path()
    if root.exists():
        shutil.rmtree(root)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db_path()}{suffix}").unlink(missing_ok=True)

    create_demo(root)
    with closing(get_connection()) as conn:
        init_db(conn)
    print(f"Created {len(SERVICES)} services and pricekit "
          f"{', '.join(v for (_, v) in LIBRARIES)} in {root}")
    print(f"Fresh database at {db_path()}")


if __name__ == "__main__":
    main()
