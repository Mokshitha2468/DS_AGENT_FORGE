import ast
import os
from pathlib import Path

app_dir = Path(r"c:\Users\user\Desktop\DS-AgentForge\DS-AgentForge\backend\app")

def get_imports(py_path: Path):
    imports = set()
    try:
        tree = ast.parse(py_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Error parsing {py_path}: {e}")
        return imports

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                imports.add(name.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module)
    return imports

# Collect all app python files
all_files = {}
for p in app_dir.rglob("*.py"):
    # compute module name
    rel = p.relative_to(app_dir.parent)
    mod_name = ".".join(rel.with_suffix("").parts)
    all_files[mod_name] = p

print(f"Total modules in app: {len(all_files)}")

# BFS from app.main
reachable = set()
queue = ["app.main"]

while queue:
    curr = queue.pop(0)
    if curr in reachable:
        continue
    reachable.add(curr)
    
    if curr in all_files:
        imports = get_imports(all_files[curr])
        for imp in imports:
            # Match directly or match prefix if importing submodule/item
            for mod in all_files:
                if mod == imp or imp.startswith(mod + ".") or mod.startswith(imp + "."):
                    if mod not in reachable and mod not in queue:
                        queue.append(mod)

unreachable = sorted(set(all_files.keys()) - reachable)
print("\nReachable count:", len(reachable))
print("Unreachable count:", len(unreachable))

print("\n--- UNREACHABLE MODULES ---")
for mod in unreachable:
    print(mod, "->", all_files[mod])

# Specifically test the ones asked in the prompt:
# backend/app/agent/nodes.py, graph.py, services/ds_native_renderer.py, datasphere_canvas_builder.py,
# api/v1/runs.py, canvas.py, export.py, views.py
check_targets = [
    "app.agent.nodes",
    "app.agent.graph",
    "app.services.ds_native_renderer",
    "app.services.datasphere_canvas_builder",
    "app.api.v1.runs",
    "app.api.v1.canvas",
    "app.api.v1.export",
    "app.api.v1.views",
]

print("\n--- TARGET STATUS ---")
for t in check_targets:
    print(f"{t}: {'UNREACHABLE (CONFIRMED)' if t in unreachable else 'REACHABLE (REFUTED)'}")
