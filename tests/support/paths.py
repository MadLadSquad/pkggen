from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "pkggen"
CLI = SRC / "pkggen"
GENERATORS = REPO / "generators"
TEMPLATES = GENERATORS / "templates"
EXAMPLE = REPO / "example"
GOLDEN = REPO / "tests" / "golden"
HTTP_SHIM = REPO / "tests" / "support" / "http_shim"
