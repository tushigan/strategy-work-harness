"""Static dependency inspection. Runtime availability is deliberately not probed."""
import ast
from pathlib import PurePosixPath
import re
import sys

from package_checks import GUIDE, REQUIRED_FILES, issue
from package_checks_content import json_value
from package_checks_references import reference


def policy(texts, result):
    text = texts.get(GUIDE, "")
    blocks = re.findall(r"```package-dependencies\s*\n(.*?)\n```", text, re.S)
    try:
        if len(blocks) != 1:
            raise ValueError("One policy block required")
        value = json_value(blocks[0])
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("Invalid policy")
        dependencies = value["external_dependencies"]
        if not isinstance(dependencies, list) or not dependencies:
            raise ValueError("Dependencies required")
        seen = set()
        for entry in dependencies:
            if not isinstance(entry, dict) or any(
                    not isinstance(entry.get(key), str) or not entry[key].strip()
                    for key in ("id", "kind", "version", "purpose")):
                raise ValueError("Invalid dependency declaration")
            if entry["kind"] not in {"runtime", "python", "node", "host", "connector", "command"}:
                raise ValueError("Invalid dependency kind")
            if entry["id"] in seen:
                raise ValueError("Duplicate dependency")
            seen.add(entry["id"])
            if entry["kind"] == "python" and not isinstance(entry.get("module"), str):
                raise ValueError("Python module missing")
        if not isinstance(value.get("bundled_assets"), list):
            raise ValueError("Asset declarations required")
        result["external_dependencies"] = dependencies
        return value
    except (ValueError, KeyError, TypeError, RecursionError):
        issue(result["errors"], "dependency_declaration", GUIDE)
        return {"external_dependencies": [], "bundled_assets": []}


def check_dependencies(texts, entries, result, declaration):
    declared = declaration["external_dependencies"]
    modules = {x["module"] for x in declared if x["kind"] == "python"}
    node_modules = {x["id"] for x in declared if x["kind"] == "node"}
    required = set(REQUIRED_FILES)
    try:
        tree = ast.parse(texts.get("scripts/validate_project.py", ""))
        for item in tree.body:
            if isinstance(item, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "REQUIRED_FILES" for t in item.targets):
                value = ast.literal_eval(item.value)
                if isinstance(value, (list, tuple)) and all(isinstance(x, str) for x in value):
                    required.update(value)
    except (ValueError, SyntaxError, RecursionError):
        issue(result["errors"], "invalid_source", "scripts/validate_project.py")
    for name in sorted(required):
        if name not in entries or entries[name]["kind"] not in {"file", "symlink"}:
            issue(result["errors"], "missing_required_file", name)

    def python_module(name, module, level=0):
        if not isinstance(module, str) or not module:
            issue(result["errors"], "unresolved_dependency", name)
            return
        first = module.split(".")[0]
        folder = PurePosixPath(name).parent
        options = [str(folder / (first + ".py")), str(folder / first / "__init__.py"),
                   "scripts/" + first + ".py", first + ".py", first + "/__init__.py"]
        if level:
            options = options[:2]
        if any(path in entries for path in options):
            return
        if not level and (first in sys.stdlib_module_names or first in modules):
            return
        issue(result["errors"], "undeclared_dependency", name)

    def javascript_module(name, module):
        if module.startswith("node:"):
            return
        if module.startswith((".", "/")):
            reference(name, module, result, entries, base=str(PurePosixPath(name).parent))
            return
        package = "/".join(module.split("/")[:2]) if module.startswith("@") else module.split("/")[0]
        if package not in node_modules:
            issue(result["errors"], "undeclared_dependency", name)

    for name, text in texts.items():
        if name.endswith(".py"):
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError, RecursionError):
                continue
            for item in ast.walk(tree):
                if isinstance(item, ast.Import):
                    for alias in item.names:
                        python_module(name, alias.name)
                elif isinstance(item, ast.ImportFrom):
                    python_module(name, item.module, item.level)
                elif isinstance(item, ast.Call):
                    function = item.func
                    method = function.attr if isinstance(function, ast.Attribute) else (
                        function.id if isinstance(function, ast.Name) else "")
                    if method in {"import_module", "__import__", "find_spec"}:
                        if item.args and isinstance(item.args[0], ast.Constant):
                            python_module(name, item.args[0].value)
                        else:
                            issue(result["errors"], "unresolved_dependency", name)
                    if method == "with_name" and item.args and isinstance(item.args[0], ast.Constant):
                        value = item.args[0].value
                        if isinstance(value, str):
                            reference(name, value, result, entries, base=str(PurePosixPath(name).parent))
        elif name.endswith((".js", ".mjs", ".cjs", ".jsx")):
            for pattern in (r"""(?m)^\s*(?:import|export)\b[^;\n]*\bfrom\s*['"]([^'"]+)['"]""",
                            r"""(?m)^\s*import\s*['"]([^'"]+)['"]""",
                            r"""(?:\brequire|\bimport|requireFrom\.resolve)\(\s*['"]([^'"]+)['"]"""):
                for match in re.finditer(pattern, text):
                    javascript_module(name, match.group(1))
            # Existing host loaders use a conditional module directory, with literal module IDs.
            for match in re.finditer(r"\brequire\(([^;]+?)\);", text, re.S):
                expression = match.group(1)
                if not expression.lstrip().startswith(("'", '"')):
                    values = re.findall(r"""['"]([^'"]+)['"]""", expression)
                    if not values:
                        issue(result["errors"], "unresolved_dependency", name)
                    for value in values:
                        javascript_module(name, value)
            for match in re.finditer(r"\bimport\(([^;\n]+)", text):
                expression = match.group(1).lstrip()
                if not expression.startswith(("'", '"', "pathToFileURL(requireFrom.resolve(")):
                    issue(result["errors"], "unresolved_dependency", name)
    ids = {x["id"].lower().replace("_", "-"): x for x in declared if x["kind"] == "python"}
    for line in texts.get("requirements.txt", "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)", line)
        entry = ids.get(match.group(1).lower().replace("_", "-")) if match else None
        if not entry or entry["version"] != match.group(2):
            issue(result["errors"], "undeclared_dependency", "requirements.txt")
