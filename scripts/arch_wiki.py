#!/usr/bin/env python3
"""
Arch Wiki scanner — endpoints, permissions, SQL queries, deployment topology, request pipeline, layers, infrastructure.

Run:
    python scripts/arch_wiki.py
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_APP = REPO_ROOT / "backend" / "app"
MAIN_FILE = BACKEND_APP / "main.py"

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}
AUTH_PATTERNS = ("admin", "auth", "permission", "role", "current_user", "verify", "api_key")
RATE_LIMIT_PATTERNS = ("rate_limit", "limiter")
SQL_KEYWORDS = ("SELECT", "INSERT", "UPDATE", "DELETE")
SQL_SCAN_ROOTS = [BACKEND_APP]
SQL_SIGNATURE_MAX = 120

OUT_FILE = REPO_ROOT / "architecture.json"


def to_rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def file_hash(path: Path) -> str:
    """Compute sha256 hash of a file for incremental scanning."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def collect_source_files() -> dict[str, Path]:
    """Collect all source files that affect architecture extraction."""
    files: dict[str, Path] = {}

    # Backend python files
    if BACKEND_APP.exists():
        for p in BACKEND_APP.rglob("*.py"):
            if "__pycache__" not in str(p):
                files[to_rel(p)] = p

    # Valuation system files
    val_sys = REPO_ROOT / "valuation_system"
    if val_sys.exists():
        for p in val_sys.rglob("*.py"):
            if "__pycache__" not in str(p):
                files[to_rel(p)] = p

    # Deployment / config files
    for name in ["render.yaml", "Procfile", "backend/Procfile", "frontend/Procfile"]:
        p = REPO_ROOT / name
        if p.exists():
            files[to_rel(p)] = p

    # Frontend .env files
    fe_dir = REPO_ROOT / "frontend"
    if fe_dir.exists():
        for p in fe_dir.glob(".env*"):
            if p.is_file():
                files[to_rel(p)] = p

    return files


def parse_main_mounts() -> dict[str, str]:
    """Find router prefixes mounted in main.py via include_router."""
    mounts: dict[str, str] = {}
    if not MAIN_FILE.exists():
        return mounts

    code = MAIN_FILE.read_text(encoding="utf-8")
    tree = ast.parse(code)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_include = False
        if isinstance(func, ast.Attribute) and func.attr == "include_router":
            is_include = True

        if not is_include:
            continue

        router_var = None
        prefix = ""

        if node.args:
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Name):
                router_var = first_arg.id
            elif isinstance(first_arg, ast.Attribute):
                router_var = first_arg.attr

        for kw in node.keywords:
            if kw.arg == "prefix" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                prefix = kw.value.value

        if router_var:
            mounts[router_var] = prefix

    return mounts


def ast_to_source(node: ast.AST, file_text: str) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        lines = file_text.splitlines()
        start = getattr(node, "lineno", 1) - 1
        end = getattr(node, "end_lineno", start + 1)
        return "\n".join(lines[start:end]).strip()


def is_auth_dependency(dep_node: ast.AST, file_text: str) -> bool:
    src = ast_to_source(dep_node, file_text).lower()
    return any(p in src for p in AUTH_PATTERNS)


def is_rate_limit_decorator(dec_node: ast.AST, file_text: str) -> bool:
    src = ast_to_source(dec_node, file_text).lower()
    return any(p in src for p in RATE_LIMIT_PATTERNS)


def endpoint_key(ep: dict[str, Any]) -> tuple[str, str, str, int]:
    return (ep["method"], ep["path"], ep["file"], ep["line"])


def parse_router_file(file_path: Path, mounts: dict[str, str]) -> list[dict[str, Any]]:
    code = file_path.read_text(encoding="utf-8")
    lines = code.splitlines()
    tree = ast.parse(code)

    file_rel = to_rel(file_path)
    stem = file_path.stem

    module_router_prefix = ""
    for var_name, prefix in mounts.items():
        if var_name == stem or var_name == f"{stem}_router" or var_name.replace("_", "") == stem.replace("_", ""):
            module_router_prefix = prefix
            break

    endpoints: list[dict[str, Any]] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if not isinstance(func, ast.Attribute):
                continue

            method = func.attr.lower()
            if method not in HTTP_METHODS:
                continue

            path_arg = ""
            if decorator.args:
                first = decorator.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    path_arg = first.value

            full_path = (module_router_prefix.rstrip("/") + "/" + path_arg.lstrip("/")).rstrip("/")
            if not full_path:
                full_path = "/"

            auth_list: list[str] = []
            rate_limit_list: list[str] = []

            for dec in node.decorator_list:
                if is_rate_limit_decorator(dec, code):
                    rate_limit_list.append(ast_to_source(dec, code))

            for kw in decorator.keywords:
                if kw.arg == "dependencies" and isinstance(kw.value, ast.List):
                    for elt in kw.value.elts:
                        if is_auth_dependency(elt, code):
                            auth_list.append(ast_to_source(elt, code))

            for arg in node.args.args + node.args.kwonlyargs:
                if arg.annotation and is_auth_dependency(arg.annotation, code):
                    auth_list.append(f"{arg.arg}: {ast_to_source(arg.annotation, code)}")

            for default in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
                if is_auth_dependency(default, code):
                    auth_list.append(ast_to_source(default, code))

            auth_dedup = sorted(set(auth_list))
            rate_dedup = sorted(set(rate_limit_list))

            module_name = f"app.api.routes.{stem}"

            endpoints.append({
                "path": full_path,
                "method": method.upper(),
                "handler": node.name,
                "file": file_rel,
                "line": node.lineno,
                "module": module_name,
                "auth": auth_dedup,
                "rate_limit": rate_dedup,
                "permission_guarded": len(auth_dedup) > 0,
            })

    return endpoints


def build_model_table_map() -> dict[str, str]:
    model_dir = BACKEND_APP / "models"
    table_map: dict[str, str] = {}
    if not model_dir.exists():
        return table_map

    for py_file in model_dir.rglob("*.py"):
        if "__pycache__" in str(py_file):
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            table_name = None
            for item in node.body:
                if isinstance(item, ast.Assign):
                    for target in item.targets:
                        if isinstance(target, ast.Name) and target.id == "__tablename__":
                            if isinstance(item.value, ast.Constant) and isinstance(item.value.value, str):
                                table_name = item.value.value
            if table_name:
                table_map[node.name] = table_name

    return table_map


def clean_signature(sig: str) -> str:
    cleaned = " ".join(sig.split())
    if len(cleaned) > SQL_SIGNATURE_MAX:
        return cleaned[: SQL_SIGNATURE_MAX - 3] + "..."
    return cleaned


def extract_sql_queries(table_map: dict[str, str]) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []

    sql_regex = re.compile(r'["\']\s*(SELECT|INSERT|UPDATE|DELETE)\b[^"\']*["\']', re.IGNORECASE | re.DOTALL)
    from_regex = re.compile(r'\bFROM\s+([a-zA-Z0-9_,\s]+?)(?:\bWHERE\b|\bJOIN\b|\bGROUP\b|\bORDER\b|\bLIMIT\b|;|$)', re.IGNORECASE)
    into_regex = re.compile(r'\bINTO\s+([a-zA-Z0-9_]+)', re.IGNORECASE)
    update_table_regex = re.compile(r'\bUPDATE\s+([a-zA-Z0-9_]+)', re.IGNORECASE)

    py_files: list[Path] = []
    for root in SQL_SCAN_ROOTS:
        if root.exists():
            py_files.extend(root.rglob("*.py"))

    for py_file in sorted(set(py_files)):
        if "__pycache__" in str(py_file):
            continue
        try:
            content = py_file.read_text(encoding="utf-8")
            tree = ast.parse(content)
        except (SyntaxError, UnicodeDecodeError):
            continue

        lines = content.splitlines()
        file_rel = to_rel(py_file)

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                func_str = ast_to_source(func, content)

                if func_str.endswith(".query") or ".query(" in func_str:
                    tables = []
                    for arg in node.args:
                        arg_str = ast_to_source(arg, content)
                        if arg_str in table_map:
                            tables.append(table_map[arg_str])
                        elif arg_str.isidentifier():
                            tables.append(arg_str.lower())
                    queries.append({
                        "file": file_rel,
                        "line": node.lineno,
                        "kind": "orm_query",
                        "operation": "SELECT",
                        "tables": sorted(set(tables)),
                        "signature": clean_signature(f"query({', '.join(ast_to_source(a, content) for a in node.args)})")
                    })

                elif func_str.endswith(".delete"):
                    tables = []
                    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                        var_name = func.value.id
                        if var_name in table_map:
                            tables.append(table_map[var_name])
                    queries.append({
                        "file": file_rel,
                        "line": node.lineno,
                        "kind": "orm_delete",
                        "operation": "DELETE",
                        "tables": sorted(set(tables)),
                        "signature": clean_signature(ast_to_source(node, content)),
                        "note": "instance type not statically resolved" if not tables else ""
                    })

                elif any(func_str.endswith(f".{op}") for op in ["execute", "fetch_all", "fetch_one", "execute_many"]):
                    if node.args:
                        first_arg = ast_to_source(node.args[0], content)
                        m = sql_regex.search(first_arg)
                        if m:
                            op = m.group(1).upper()
                            raw_sql = m.group(0).strip("\"'")
                            tables = []
                            if op == "SELECT":
                                fm = from_regex.search(raw_sql)
                                if fm:
                                    t_str = fm.group(1)
                                    tables = [t.strip() for t in t_str.split(",") if t.strip()]
                            elif op == "INSERT":
                                im = into_regex.search(raw_sql)
                                if im:
                                    tables = [im.group(1)]
                            elif op == "UPDATE":
                                um = update_table_regex.search(raw_sql)
                                if um:
                                    tables = [um.group(1)]

                            queries.append({
                                "file": file_rel,
                                "line": node.lineno,
                                "kind": "raw_execute",
                                "operation": op,
                                "tables": sorted(set(tables)),
                                "signature": clean_signature(raw_sql)
                            })

                elif func_str in ("insert", "update", "delete", "select") or func_str.endswith((".insert", ".update", ".delete", ".select")):
                    if node.args:
                        first_arg = ast_to_source(node.args[0], content)
                        tables = []
                        if first_arg in table_map:
                            tables.append(table_map[first_arg])
                        elif first_arg.isidentifier():
                            tables.append(first_arg.lower())
                        queries.append({
                            "file": file_rel,
                            "line": node.lineno,
                            "kind": "sa_core",
                            "operation": func_str.split(".")[-1].upper(),
                            "tables": sorted(set(tables)),
                            "signature": clean_signature(ast_to_source(node, content))
                        })

        for match in sql_regex.finditer(content):
            sql_str = match.group(0).strip("\"'")
            op = match.group(1).upper()
            line_no = content[:match.start()].count("\n") + 1

            if any(q["file"] == file_rel and abs(q["line"] - line_no) <= 2 for q in queries):
                continue

            tables = []
            if op == "SELECT":
                fm = from_regex.search(sql_str)
                if fm:
                    tables = [t.strip() for t in fm.group(1).split(",") if t.strip()]
            elif op == "INSERT":
                im = into_regex.search(sql_str)
                if im:
                    tables = [im.group(1)]
            elif op == "UPDATE":
                um = update_table_regex.search(sql_str)
                if um:
                    tables = [um.group(1)]

            queries.append({
                "file": file_rel,
                "line": line_no,
                "kind": "raw_sql",
                "operation": op,
                "tables": sorted(set(tables)),
                "signature": clean_signature(sql_str)
            })

    queries.sort(key=lambda q: (q["file"], q["line"], q["kind"]))
    return queries


def extract_procfile(path: Path) -> dict[str, Any]:
    processes: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and ":" in line:
                name, cmd = line.split(":", 1)
                processes[name.strip()] = cmd.strip()
    except OSError:
        pass
    return {"path": to_rel(path), "processes": processes}


def extract_render_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"services": []}
    
    services: list[dict[str, Any]] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    
    current_service: dict[str, Any] | None = None
    in_envVars = False
    current_env: dict[str, Any] | None = None
    
    for line in lines:
        if not line.strip() or line.strip().startswith("#"):
            continue
            
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        
        if stripped == "services:":
            continue
        elif indent == 2 and stripped.startswith("- "):
            current_service = {"envVars": []}
            services.append(current_service)
            in_envVars = False
            rest = stripped[2:].strip()
            if ":" in rest:
                k, v = rest.split(":", 1)
                current_service[k.strip()] = v.strip()
        elif indent == 4 and current_service is not None:
            if stripped == "envVars:":
                in_envVars = True
            elif ":" in stripped and not in_envVars:
                k, v = stripped.split(":", 1)
                current_service[k.strip()] = v.strip()
        elif indent == 6 and in_envVars and current_service is not None:
            if stripped.startswith("- "):
                current_env = {}
                current_service["envVars"].append(current_env)
                rest = stripped[2:].strip()
                if ":" in rest:
                    k, v = rest.split(":", 1)
                    current_env[k.strip()] = v.strip()
            elif ":" in stripped and current_env is not None:
                k, v = stripped.split(":", 1)
                current_env[k.strip()] = v.strip()
                
    return {"services": services}


def extract_deployment_topology() -> dict[str, Any]:
    procfiles = []
    for proc in REPO_ROOT.rglob("Procfile"):
        if "venv" not in str(proc) and "node_modules" not in str(proc):
            procfiles.append(extract_procfile(proc))
    
    render_yaml_data = extract_render_yaml(REPO_ROOT / "render.yaml")
    
    return {
        "procfiles": sorted(procfiles, key=lambda x: x["path"]),
        "render_yaml": render_yaml_data
    }


def extract_request_pipeline() -> dict[str, Any]:
    pipeline = {
        "middlewares": [],
        "exception_handlers": [],
        "notes": [
            "Auth and Validation are handled per-endpoint via FastAPI Depends (see api_endpoints)."
        ]
    }
    
    main_py = BACKEND_APP / "main.py"
    if not main_py.exists():
        return pipeline
        
    tree = ast.parse(main_py.read_text(encoding="utf-8"))
    
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func_str = ast_to_source(node.func, main_py.read_text(encoding="utf-8"))
            if func_str.endswith(".add_middleware") and node.args:
                mw_name = ast_to_source(node.args[0], main_py.read_text(encoding="utf-8"))
                pipeline["middlewares"].append(mw_name)
            elif func_str.endswith(".add_exception_handler") and len(node.args) >= 2:
                exc_name = ast_to_source(node.args[0], main_py.read_text(encoding="utf-8"))
                handler_name = ast_to_source(node.args[1], main_py.read_text(encoding="utf-8"))
                pipeline["exception_handlers"].append({
                    "exception": exc_name,
                    "handler": handler_name
                })
                
    return pipeline


def extract_core_layers() -> dict[str, Any]:
    """Map top-level directories to their architectural role."""
    layer_defs: list[tuple[str, str, str]] = [
        ("backend/app/api/routes", "routes", "FastAPI route handlers — endpoint definitions"),
        ("backend/app/api", "api", "API layer — routers, dependencies"),
        ("backend/app/core", "core", "Core config, auth, security, database, redis, CSRF, rate limiting"),
        ("backend/app/models", "models", "SQLAlchemy ORM models"),
        ("backend/app/schemas", "schemas", "Pydantic request/response schemas"),
        ("backend/app/services", "services", "Business logic — RS ratings, SATA, valuation, scrapers"),
        ("backend/app/scrapers", "scrapers", "External data scrapers — Tadawul, FRED, Yahoo, NAAIM, CME"),
        ("backend/app/utils", "utils", "Backend utilities"),
        ("backend/app/wallet", "wallet", "Wallet/payment module"),
        ("frontend/app", "pages", "Next.js app router — pages and layouts"),
        ("frontend/components", "components", "React UI components"),
        ("frontend/hooks", "hooks", "Custom React hooks"),
        ("frontend/lib", "lib", "Frontend library utilities"),
        ("frontend/store", "store", "Client-side state management"),
        ("frontend/types", "types", "TypeScript type definitions"),
        ("frontend/utils", "utils_fe", "Frontend utilities"),
        ("valuation_system", "valuation", "Standalone valuation engine — in-process, no HTTP"),
        ("scripts", "scripts", "Dev/build/analysis scripts"),
    ]
    layers: list[dict[str, Any]] = []
    for rel, role, desc in layer_defs:
        p = REPO_ROOT / rel
        if p.exists() and p.is_dir():
            file_count = sum(1 for _ in p.rglob("*") if _.is_file()
                             and "__pycache__" not in str(_)
                             and "node_modules" not in str(_)
                             and ".next" not in str(_))
            layers.append({
                "path": rel.replace("\\", "/"),
                "role": role,
                "description": desc,
                "file_count": file_count,
            })
    return {"layers": layers}


def extract_infrastructure() -> dict[str, Any]:
    """Extract env var names (no values!) and external service domains."""
    env_vars: set[str] = set()
    external_domains: set[str] = set()

    getenv_re = re.compile(r'os\.(?:getenv|environ\.get)\(\s*["\']([A-Z_][A-Z0-9_]*)["\']')
    url_re = re.compile(r'https?://([a-zA-Z0-9.-]+)')
    skip_domains = {"localhost", "127.0.0.1", "0.0.0.0", "example.com"}

    for py_file in sorted(BACKEND_APP.rglob("*.py")):
        if "__pycache__" in str(py_file):
            continue
        try:
            content = py_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in getenv_re.finditer(content):
            env_vars.add(m.group(1))
        for m in url_re.finditer(content):
            domain = m.group(1).lower().rstrip(".")
            if domain not in skip_domains and not domain.startswith("127."):
                parts = domain.split(".")
                if len(parts) > 2:
                    base = ".".join(parts[-2:])
                else:
                    base = domain
                external_domains.add(base)

    fe_dir = REPO_ROOT / "frontend"
    for env_file in sorted(fe_dir.glob(".env*")):
        if env_file.is_file():
            try:
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        var_name = line.split("=", 1)[0].strip()
                        if var_name:
                            env_vars.add(var_name)
            except OSError:
                continue

    return {
        "env_var_names": sorted(env_vars),
        "external_services": sorted(external_domains),
        "note": "Only variable names are captured, never values/secrets."
    }


def pick_sample_by_kind(queries: list[dict[str, Any]], kinds: list[str]) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for q in queries:
        kind = q["kind"]
        if kind in kinds and kind not in found:
            found[kind] = q
        if len(found) == len(kinds):
            break
    return [found[k] for k in kinds if k in found]


def run() -> int:
    # ── Incremental hashing ────────────────────────────────────────
    source_files = collect_source_files()
    current_hashes: dict[str, str] = {rel: file_hash(abspath) for rel, abspath in source_files.items()}

    prev_data: dict[str, Any] = {}
    prev_hashes: dict[str, str] = {}
    if OUT_FILE.exists():
        try:
            prev_data = json.loads(OUT_FILE.read_text(encoding="utf-8"))
            prev_hashes = prev_data.get("_meta", {}).get("file_hashes", {})
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    changed = {rel for rel, h in current_hashes.items() if prev_hashes.get(rel) != h}
    unchanged = set(current_hashes) - changed

    if not changed and prev_data.get("api_endpoints"):
        print(f"Incremental scan: 0 files changed, {len(unchanged)} unchanged — nothing to do.")
        print(f"Reusing previous output: {to_rel(OUT_FILE)}")
        return 0

    print(f"Incremental scan: {len(changed)} file(s) changed, {len(unchanged)} unchanged.")
    if len(changed) <= 10:
        for c in sorted(changed):
            print(f"  ↻ {c}")

    mounts = parse_main_mounts()
    route_files = sorted((BACKEND_APP / "api" / "routes").rglob("*.py"))
    endpoints: list[dict[str, Any]] = []
    for rf in route_files:
        endpoints.extend(parse_router_file(rf, mounts))

    endpoints.sort(key=lambda e: (e["path"], e["method"], e["file"], e["line"]))
    permissions_count = sum(1 for e in endpoints if e["permission_guarded"])

    previous_endpoints: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    for ep in prev_data.get("api_endpoints", []):
        try:
            previous_endpoints[endpoint_key(ep)] = ep
        except (KeyError, TypeError):
            pass

    newly_guarded: list[dict[str, Any]] = []
    for ep in endpoints:
        old = previous_endpoints.get(endpoint_key(ep))
        if old and not old.get("permission_guarded") and ep["permission_guarded"]:
            newly_guarded.append(ep)

    table_map = build_model_table_map()
    sql_queries = extract_sql_queries(table_map)
    tables_touched = sorted({t for q in sql_queries for t in q["tables"]})

    deployment_topology = extract_deployment_topology()
    deployment_topology["note"] = (
        "Additional environment variables (like DATABASE_URL, REDIS_URL, or secrets) "
        "may be configured directly in the Render dashboard and will not appear in render.yaml. "
        "Do not assume the app lacks database config just because it is missing here."
    )

    request_pipeline = extract_request_pipeline()
    core_layers = extract_core_layers()
    infrastructure = extract_infrastructure()

    out = {
        "_meta": {
            "schema_version": "1.0",
            "last_scan": datetime.now(timezone.utc).isoformat(),
            "stage": "step6_architecture_json",
            "models_mapped": len(table_map),
            "file_hashes": current_hashes,
        },
        "core_layers": core_layers,
        "deployment_topology": deployment_topology,
        "request_pipeline": request_pipeline,
        "infrastructure": infrastructure,
        "api_endpoints": endpoints,
        "sql_queries": sql_queries,
    }
    OUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    out_size_kb = OUT_FILE.stat().st_size / 1024

    # ── Generate dashboard HTML with embedded data ──────────────
    dashboard_template = REPO_ROOT / "architecture-dashboard.html"
    if dashboard_template.exists():
        html = dashboard_template.read_text(encoding="utf-8")
        json_compact = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
        embedded_script = f"const __ARCH_DATA__ = {json_compact};"
        if "/* __ARCH_DATA_PLACEHOLDER__ */" in html:
            html = html.replace("/* __ARCH_DATA_PLACEHOLDER__ */", embedded_script)
        else:
            # Fallback regex replacement if placeholder was already replaced
            html = re.sub(r'const __ARCH_DATA__ = \{.*?\};', embedded_script, html, flags=re.DOTALL)
        dashboard_template.write_text(html, encoding="utf-8")
        dash_kb = dashboard_template.stat().st_size / 1024
        print(f"Dashboard updated: {dash_kb:.0f} KB -> {to_rel(dashboard_template)}")

    print(f"Extracted {len(endpoints)} endpoints from {len(route_files)} route files.")
    print(f"Permission-guarded endpoints: {permissions_count}")
    if previous_endpoints:
        prev_guarded = sum(1 for ep in previous_endpoints.values() if ep.get("permission_guarded"))
        print(f"Previous permission-guarded endpoints: {prev_guarded} (delta: {permissions_count - prev_guarded:+d})")
    print(f"Extracted {len(sql_queries)} SQL/ORM queries ({len(tables_touched)} tables).")

    print("\n--- Deployment Topology ---")
    print(f"Found {len(deployment_topology['procfiles'])} Procfile(s):")
    for p in deployment_topology["procfiles"]:
        print(f"  - {p['path']}: {list(p['processes'].keys())}")

    print("render.yaml services:")
    for svc in deployment_topology["render_yaml"]["services"]:
        env_vars = svc.get("envVars", [])
        print(f"  - Service '{svc.get('name', 'unknown')}' ({svc.get('type', 'unknown')}): {len(env_vars)} envVars")
        for env in env_vars:
            print(f"      {env}")

    print("\n--- Request Pipeline ---")
    print(f"Middlewares: {', '.join(request_pipeline['middlewares'])}")
    print(f"Exception Handlers: {len(request_pipeline['exception_handlers'])}")
    for eh in request_pipeline['exception_handlers']:
        print(f"  - {eh['exception']} -> {eh['handler']}")

    print(f"\n--- Core Layers ---")
    for layer in core_layers["layers"]:
        print(f"  {layer['path']:40s}  [{layer['role']}] {layer['file_count']} files")

    print(f"\n--- Infrastructure ---")
    print(f"Env vars detected: {len(infrastructure['env_var_names'])}")
    print(f"External service domains: {len(infrastructure['external_services'])}")
    for d in infrastructure['external_services']:
        print(f"  - {d}")

    print(f"\nWrote {out_size_kb:.0f} KB -> {to_rel(OUT_FILE)}")

    if newly_guarded:
        print(f"\nNewly permission-guarded endpoints ({len(newly_guarded)}):")
        for ep in newly_guarded:
            auth = ", ".join(ep["auth"]) if ep["auth"] else "none"
            print(
                f"- {ep['method']} {ep['path']} -> {ep['handler']} "
                f"({ep['file']}:{ep['line']}) | auth: {auth}"
            )
    else:
        print("\nNo newly permission-guarded endpoints vs previous sample output.")

    print("\nSample SQL queries (one per kind):")
    sql_kinds = ["orm_query", "raw_sql", "raw_execute", "orm_delete", "sa_core"]
    for q in pick_sample_by_kind(sql_queries, sql_kinds):
        tables = ", ".join(q["tables"]) if q["tables"] else "unknown"
        extra = ""
        if "defined_at" in q:
            extra += f" | defined_at: {q['defined_at']}"
        if "note" in q:
            extra += f" | note: {q['note']}"
        print(
            f"- [{q['kind']}] {q['operation']} on {tables} "
            f"({q['file']}:{q['line']}){extra}"
        )
        print(f"    {q['signature']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
