"""``docs/providers.md``: every cell of the provider matrix is backed by something checkable.

A cell opens with its status. "Supported" and "Not supported" name a file of this
repository that shows it (a test, a recorded fixture, a Terraform root), so no cell
rests on a provider's documentation alone. "Not built" and "Planned" name the issue
that tracks the gap. A cited file must exist, and a cited ``path::A::b`` must name a
class or function ``A`` defined at the top of that file and ``b`` defined in ``A``.
"""

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
MATRIX = REPO / "docs" / "providers.md"

GAPS_SECTION = "Gaps between Keycloak and Zitadel"
PROVIDERS = ("Descope", "Ory", "Keycloak", "Zitadel", "Cloudflare Access")
PLANNED_PROVIDER = "Cloudflare Access"
SECTIONS = (
    "Tokens",
    "Tenancy",
    "Authorization",
    "Admin operations",
    "Flows and protocol features",
    "Operations",
    "Test coverage",
)

SUPPORTED = "Supported"
NOT_SUPPORTED = "Not supported"
NOT_BUILT = "Not built"
NOT_APPLICABLE = "Not applicable"
PLANNED = "Planned"
STATUSES = (NOT_SUPPORTED, NOT_BUILT, NOT_APPLICABLE, PLANNED, SUPPORTED)
NEEDS_A_FILE = (SUPPORTED, NOT_SUPPORTED)
NEEDS_AN_ISSUE = (NOT_BUILT, PLANNED)

CODE_SPAN = re.compile(r"`([^`]+)`")
ISSUE = re.compile(r"#\d+")
# A repository path, optionally followed by ``::Name`` parts: no scheme, no
# leading slash, no placeholder, at least one directory or a file extension.
PATH = re.compile(r"^(?P<path>[\w.-]+(?:/[\w.-]+)*\.[\w]+|[\w.-]+(?:/[\w.-]+)+/?)(?P<names>(?:::\w+)*)$")


@dataclass(frozen=True)
class Cell:
    section: str
    row: str
    provider: str
    text: str

    @property
    def where(self) -> str:
        return f"{self.section} / {self.row} / {self.provider}"

    @property
    def status(self) -> str | None:
        return next((status for status in STATUSES if self.text.startswith(status)), None)


def _cells_of(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = sections.setdefault(line[3:].strip(), [])
        elif current is not None:
            current.append(line)
    return sections


def _matrix_tables(lines: list[str]) -> list[list[list[str]]]:
    """The tables of ``lines`` whose header names the providers, as rows of cells, body only."""
    tables: list[list[list[str]]] = []
    block: list[str] = []
    for line in [*lines, ""]:
        if line.startswith("|"):
            block.append(line)
            continue
        if len(block) > 2 and tuple(_cells_of(block[0])[1:]) == PROVIDERS:
            tables.append([_cells_of(row) for row in block[2:]])
        block = []
    return tables


def _matrix() -> dict[str, list[Cell]]:
    sections = _sections(MATRIX.read_text(encoding="utf-8"))
    cells: dict[str, list[Cell]] = {}
    for section in SECTIONS:
        for table in _matrix_tables(sections.get(section, [])):
            for row in table:
                label, *values = row
                cells.setdefault(section, []).extend(
                    Cell(section, label, provider, value) for provider, value in zip(PROVIDERS, values, strict=False)
                )
                if len(values) != len(PROVIDERS):
                    cells[section].append(Cell(section, label, "(column count)", ""))
    return cells


def _all_cells() -> list[Cell]:
    return [cell for section in _matrix().values() for cell in section]


def _paths(text: str) -> list[tuple[str, list[str]]]:
    found = []
    for span in CODE_SPAN.findall(text):
        match = PATH.match(span)
        if match:
            names = [name for name in match.group("names").split("::") if name]
            found.append((match.group("path"), names))
    return found


DEFINITIONS = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _missing(path: str, names: list[str]) -> list[str]:
    """What of ``path::names[0]::names[1]...`` is not there, each name looked up inside the one before."""
    target = REPO / path
    if not target.exists():
        return [f"{path} does not exist"]
    if not names:
        return []
    if not target.is_file():
        return [f"{path} is not a file, so it defines no {'::'.join(names)}"]
    body: list[ast.stmt] = ast.parse(target.read_text(encoding="utf-8")).body
    found = path
    for name in names:
        definition = next((node for node in body if isinstance(node, DEFINITIONS) and node.name == name), None)
        if definition is None:
            return [f"{found}::{name} is not defined there"]
        found = f"{found}::{name}"
        body = definition.body
    return []


def test_the_document_exists():
    assert MATRIX.is_file()


@pytest.mark.parametrize("section", SECTIONS)
def test_every_area_of_the_scope_has_a_table_with_one_column_per_provider(section):
    assert _matrix().get(section), f"## {section} has no table with the columns {PROVIDERS}"


def test_every_row_has_one_cell_per_provider():
    assert [cell.where for cell in _all_cells() if cell.provider == "(column count)"] == []


def test_every_cell_opens_with_its_status():
    assert [f"{cell.where}: {cell.text!r}" for cell in _all_cells() if cell.status is None] == []


def _gives_no_reason(cell: Cell) -> bool:
    return cell.status == NOT_APPLICABLE and not cell.text.removeprefix(NOT_APPLICABLE).lstrip(":").strip()


def test_a_not_applicable_cell_gives_its_reason():
    assert [cell.where for cell in _all_cells() if _gives_no_reason(cell)] == []


def test_a_supported_or_not_supported_cell_names_a_file_of_this_repository():
    unbacked = [cell.where for cell in _all_cells() if cell.status in NEEDS_A_FILE and not _paths(cell.text)]
    assert unbacked == []


def test_a_not_built_or_planned_cell_names_its_issue():
    untracked = [cell.where for cell in _all_cells() if cell.status in NEEDS_AN_ISSUE and not ISSUE.search(cell.text)]
    assert untracked == []


def test_every_cited_file_exists_and_every_cited_name_is_defined_in_it():
    missing = [
        f"{cell.where}: {problem}"
        for cell in _all_cells()
        for path, names in _paths(cell.text)
        for problem in _missing(path, names)
    ]
    assert missing == []


def test_the_planned_provider_is_planned_in_every_cell():
    assert [cell.where for cell in _all_cells() if cell.provider == PLANNED_PROVIDER and cell.status != PLANNED] == []


def _unexplained_differences(cells: list[Cell], gaps: str) -> list[str]:
    """Rows where Keycloak and Zitadel differ in status that ``gaps`` names neither by issue nor by row label."""
    rows: dict[tuple[str, str], dict[str, Cell]] = {}
    for cell in cells:
        rows.setdefault((cell.section, cell.row), {})[cell.provider] = cell
    mentioned = set(ISSUE.findall(gaps))
    unexplained = []
    for (section, row), by_provider in rows.items():
        keycloak, zitadel = by_provider.get("Keycloak"), by_provider.get("Zitadel")
        if keycloak is None or zitadel is None or keycloak.status == zitadel.status:
            continue
        issues = set(ISSUE.findall(keycloak.text)) | set(ISSUE.findall(zitadel.text))
        if not issues & mentioned and row.replace("`", "").lower() not in gaps.replace("`", "").lower():
            unexplained.append(f"{section} / {row}")
    return unexplained


def test_every_difference_between_keycloak_and_zitadel_is_explained():
    gaps = "\n".join(_sections(MATRIX.read_text(encoding="utf-8")).get(GAPS_SECTION, []))
    assert gaps, f"## {GAPS_SECTION} is missing"
    assert _unexplained_differences(_all_cells(), gaps) == []


@pytest.mark.parametrize("index", ["README.md", "docs/README.md"])
def test_the_document_is_linked(index):
    assert "providers.md" in (REPO / index).read_text(encoding="utf-8")


class TestTheChecksThemselves:
    """The parser finds what a reader of the table sees."""

    def test_a_path_and_its_names_are_read_from_a_code_span(self):
        assert _paths("Supported: `backend/tests/unit/test_x.py::TestA::test_b`, `GET /api/identity`") == [
            ("backend/tests/unit/test_x.py", ["TestA", "test_b"])
        ]

    @pytest.mark.parametrize(
        "span",
        ["https://api.descope.com/{pid}", "<KC_HOSTNAME>/realms/<realm>", "/api/identity", "X-Tenant-ID", "RS256"],
    )
    def test_a_url_a_route_or_a_value_is_not_a_path(self, span):
        assert _paths(f"`{span}`") == []

    def test_a_missing_file_and_a_missing_name_are_reported(self):
        assert _missing("backend/tests/unit/no_such_file.py", []) == [
            "backend/tests/unit/no_such_file.py does not exist"
        ]
        assert _missing("backend/tests/unit/test_provider_matrix.py", ["TestNoSuchClass"]) == [
            "backend/tests/unit/test_provider_matrix.py::TestNoSuchClass is not defined there"
        ]

    def test_a_defined_name_is_found(self):
        assert _missing("backend/tests/unit/test_provider_matrix.py", ["_missing"]) == []
        assert (
            _missing(
                "backend/tests/unit/test_provider_matrix.py",
                ["TestTheChecksThemselves", "test_a_defined_name_is_found"],
            )
            == []
        )

    def test_a_name_is_looked_up_inside_the_one_before_it(self):
        assert _missing("backend/tests/unit/test_provider_matrix.py", ["TestTheChecksThemselves", "_missing"]) == [
            "backend/tests/unit/test_provider_matrix.py::TestTheChecksThemselves::_missing is not defined there"
        ]
        assert _missing("backend/tests/unit/test_provider_matrix.py", ["test_a_defined_name_is_found"]) == [
            "backend/tests/unit/test_provider_matrix.py::test_a_defined_name_is_found is not defined there"
        ]

    def test_a_directory_defines_no_name(self):
        assert _missing("backend/tests/unit", ["Anything"]) == [
            "backend/tests/unit is not a file, so it defines no Anything"
        ]

    def test_a_not_applicable_cell_without_a_reason_is_found(self):
        assert _gives_no_reason(Cell("s", "r", "p", "Not applicable"))
        assert _gives_no_reason(Cell("s", "r", "p", "Not applicable: "))
        assert not _gives_no_reason(Cell("s", "r", "p", "Not applicable: hosted only"))

    def test_a_difference_is_explained_by_its_issue_or_its_row_label(self):
        def row(label: str, keycloak: str, zitadel: str) -> list[Cell]:
            return [Cell("S", label, "Keycloak", keycloak), Cell("S", label, "Zitadel", zitadel)]

        cells = [
            *row("Same", "Supported (`a`)", "Supported (`b`)"),
            *row("By issue", "Supported (`a`)", "Not built (#1)"),
            *row("`By name`", "Supported (`a`)", "Not applicable: by design"),
            *row("Unexplained", "Supported (`a`)", "Not built (#2)"),
            *row("Also unexplained", "Supported (`a`)", "Not applicable: by design"),
            Cell("S", "Short row", "Keycloak", "Supported (`a`)"),
        ]
        assert _unexplained_differences(cells, "Built for one only (#1). By design: `by name`.") == [
            "S / Unexplained",
            "S / Also unexplained",
        ]

    def test_a_status_must_open_the_cell(self):
        assert Cell("s", "r", "p", "Not built (#1)").status == NOT_BUILT
        assert Cell("s", "r", "p", "Not supported: x").status == NOT_SUPPORTED
        assert Cell("s", "r", "p", "Works: `x`").status is None

    def test_only_a_table_naming_the_providers_is_read(self):
        lines = [
            "| Row | " + " | ".join(PROVIDERS) + " |",
            "|---|---|---|---|---|---|",
            "| a | 1 | 2 | 3 | 4 | 5 |",
            "",
            "| Other | x |",
            "|---|---|",
            "| b | y |",
        ]
        assert _matrix_tables(lines) == [[["a", "1", "2", "3", "4", "5"]]]
