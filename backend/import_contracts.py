"""import-linter contract type for the layer rules in docs/conventions.md §26.

Registered in pyproject.toml under ``[tool.importlinter] contract_types``.
"""

from __future__ import annotations

from grimp import ImportGraph
from importlinter import Contract, ContractCheck, fields, output


class ContextLayersContract(Contract):
    """Forbid direct imports between packages, except from unmigrated bounded contexts.

    ``rules`` holds ``importer -> forbidden`` pairs: no module in the importer
    package may directly import the forbidden package. ``unmigrated`` holds one
    entry per bounded context, ``<context>: <module> <module> ...``, naming the
    modules exempt from every rule until that context is migrated.

    An exempt module that breaks no rule, or does not exist, breaks the contract,
    so an entry cannot outlive the code it excuses.
    """

    type_name = "context_layers"

    rules = fields.ListField(subfield=fields.ImportExpressionField())
    unmigrated = fields.ListField(subfield=fields.StringField(), required=False)

    def check(self, graph: ImportGraph, verbose: bool) -> ContractCheck:
        exempt = {}
        for entry in self.unmigrated or []:
            context, _, modules = entry.partition(":")
            for module in modules.split():
                exempt[module] = context.strip()

        violations = []
        offenders = set()
        for rule in self.rules:
            importer_package = rule.importer.expression
            forbidden_package = rule.imported.expression
            for importer in sorted(_package(graph, importer_package)):
                for imported in sorted(graph.find_modules_directly_imported_by(importer)):
                    if not _within(imported, forbidden_package):
                        continue
                    offenders.add(importer)
                    if importer not in exempt:
                        violations.append((importer, imported))

        stale = sorted((context, module) for module, context in exempt.items() if module not in offenders)
        return ContractCheck(kept=not violations and not stale, metadata={"violations": violations, "stale": stale})

    def render_broken_contract(self, check: ContractCheck) -> None:
        for importer, imported in check.metadata["violations"]:
            output.print_error(f"{importer} is not allowed to import {imported}", bold=False)
        for context, module in check.metadata["stale"]:
            output.print_error(f"{module} breaks no rule: remove it from the '{context}' entry", bold=False)


def _package(graph: ImportGraph, package: str) -> set[str]:
    if package not in graph.modules:
        return set()
    return {package} | graph.find_descendants(package)


def _within(module: str, package: str) -> bool:
    return module == package or module.startswith(package + ".")
