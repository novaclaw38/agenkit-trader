import ast
import subprocess
import sys
from pathlib import Path

import agenkit_trader.ui as ui_pkg


def test_no_forbidden_modules_loaded_in_fresh_interpreter() -> None:
    code = (
        "import sys\n"
        "import agenkit_trader.ui.server, agenkit_trader.ui.readers, "
        "agenkit_trader.ui.page\n"
        "bad = [m for m in sys.modules if m == 'httpx' or m == 'requests' or "
        "m.startswith(('agenkit_trader.live_unlock', 'agenkit_trader.runtime', "
        "'agenkit_trader.killswitch', "
        "'agenkit_trader.risk', 'agenkit_trader.cli', "
        "'agenkit_trader.execution.binance'))]\n"
        "ex = sorted(m for m in sys.modules if "
        "m.startswith('agenkit_trader.execution'))\n"
        "print(bad, ex)\n"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out.startswith("[]"), out
    # only the pure interface module (pulled in by config) is allowed
    assert "binance" not in out and "'agenkit_trader.execution.venue'" in out


def test_ui_source_imports_are_clean() -> None:
    pkg_dir = Path(ui_pkg.__file__).parent
    banned = (
        "agenkit_trader.execution.binance_testnet",
        "agenkit_trader.live_unlock",
        "agenkit_trader.runtime",
        "agenkit_trader.killswitch",
        "agenkit_trader.risk",
        "agenkit_trader.cli",
        "httpx",
        "requests",
        "os",  # no environment access at all
        "subprocess",
        "threading",
    )
    for f in pkg_dir.glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                assert not n.startswith(banned), f"{f.name} imports {n}"
        assert "environ" not in f.read_text(), f.name
