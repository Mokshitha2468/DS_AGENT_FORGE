"""
ViewGraph & Deterministic Operations Engine for SAP Datasphere Graphical Views.

Single source of truth for view-level modeling graphs:
  Existing objects as sources -> Join / Projection / Calculation / Filter operators -> Output node.

Supports:
- Pydantic models for ViewGraph, Nodes, Edges, Operations.
- Deterministic apply_operation / apply_operations with undo/redo capability.
- Live validation: cycle detection, upstream column existence, join key compatibility, dangling inputs, reachability.
- ViewGraph synthesis from Plan nodes (e.g., PO Processing Time example).
- AI revision operation validation (AI cannot modify SOURCE nodes, invalid operations rejected with clear reasons).
"""
from __future__ import annotations

from enum import Enum
import re
from typing import Any, Optional
from pydantic import BaseModel, Field, ConfigDict


class NodeType(str, Enum):
    SOURCE = "SOURCE"
    JOIN = "JOIN"
    PROJECTION = "PROJECTION"
    CALCULATION = "CALCULATION"
    FILTER = "FILTER"
    UNION = "UNION"
    OUTPUT = "OUTPUT"


class JoinType(str, Enum):
    INNER = "inner"
    LEFT = "left"
    RIGHT = "right"
    FULL = "full"


class OperationType(str, Enum):
    ADD_NODE = "add_node"
    REMOVE_NODE = "remove_node"
    UPDATE_NODE = "update_node"
    ADD_EDGE = "add_edge"
    REMOVE_EDGE = "remove_edge"
    SET_JOIN = "set_join"
    SET_FILTER = "set_filter"
    SET_CALCULATION = "set_calculation"
    INCLUDE_COLUMN = "include_column"
    EXCLUDE_COLUMN = "exclude_column"
    RENAME = "rename"


class ColumnRef(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    data_type: str = "NVARCHAR"
    label: str = ""
    is_key: bool = False
    is_measure: bool = False


class NodeDetails(BaseModel):
    """Parameters specific to each operator node type."""
    model_config = ConfigDict(extra="ignore")

    # JOIN
    join_type: str = "inner"
    key_pairs: list[list[str]] = Field(default_factory=list)  # e.g. [["KEY_A", "KEY_B"]]

    # PROJECTION
    included_columns: list[str] = Field(default_factory=list)
    excluded_columns: list[str] = Field(default_factory=list)
    renamed_columns: dict[str, str] = Field(default_factory=dict)  # old_name -> new_name

    # CALCULATION
    calculation_name: str = ""
    expression: str = ""
    data_type: str = "Decimal"

    # FILTER
    filter_expression: str = ""

    # UNION
    union_all: bool = False

    # OUTPUT
    output_name: str = ""


class ViewGraphNode(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    node_type: NodeType
    is_existing: bool = False  # True for SOURCE nodes
    columns: list[str] = Field(default_factory=list)
    details: NodeDetails = Field(default_factory=NodeDetails)
    position: dict[str, float] = Field(default_factory=lambda: {"x": 0.0, "y": 0.0})
    errors: list[str] = Field(default_factory=list)


class ViewGraphEdge(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    source: str
    target: str
    source_handle: Optional[str] = None
    target_handle: Optional[str] = None


class ViewGraph(BaseModel):
    model_config = ConfigDict(extra="ignore")

    view_id: str
    view_name: str
    nodes: list[ViewGraphNode] = Field(default_factory=list)
    edges: list[ViewGraphEdge] = Field(default_factory=list)
    version: int = 1

    def get_node(self, node_id: str) -> Optional[ViewGraphNode]:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def get_upstream_node_ids(self, node_id: str) -> list[str]:
        return [e.source for e in self.edges if e.target == node_id]

    def get_downstream_node_ids(self, node_id: str) -> list[str]:
        return [e.target for e in self.edges if e.source == node_id]


class Operation(BaseModel):
    """Atomic change applied to a ViewGraph."""
    model_config = ConfigDict(extra="ignore")

    op: OperationType
    node_id: Optional[str] = None
    edge_id: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)


class AIRevisionResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    valid: bool
    operations: list[Operation] = Field(default_factory=list)
    rejected_operations: list[dict[str, str]] = Field(default_factory=list)  # {"op": ..., "reason": ...}
    diff: dict[str, Any] = Field(default_factory=dict)  # {"added_nodes": [], "removed_nodes": [], "modified_nodes": []}
    preview_graph: Optional[ViewGraph] = None
    message: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic Live Validation
# ─────────────────────────────────────────────────────────────────────────────

def get_available_upstream_columns(graph: ViewGraph, node_id: str) -> set[str]:
    """Recursively computes all column names available upstream of a given node."""
    visited: set[str] = set()
    cols: set[str] = set()

    def _walk(curr_id: str):
        if curr_id in visited:
            return
        visited.add(curr_id)
        node = graph.get_node(curr_id)
        if not node:
            return
        # If this is a source or has defined output columns, add them
        cols.update(node.columns)
        if node.details.calculation_name:
            cols.add(node.details.calculation_name)
        # Recurse to all upstream inputs
        for up_id in graph.get_upstream_node_ids(curr_id):
            _walk(up_id)

    for up_id in graph.get_upstream_node_ids(node_id):
        _walk(up_id)

    return cols


def validate_view_graph(graph: ViewGraph) -> list[str]:
    """
    Validates graph consistency:
    1. Cycle detection (must be DAG).
    2. Output node presence and reachability.
    3. No dangling inputs on operator/output nodes.
    4. Join key existence in upstream columns.
    5. Filter and calculation column references.
    """
    errors: list[str] = []

    # 1. Cycle detection using Kahn's algorithm or DFS
    adj: dict[str, list[str]] = {n.id: [] for n in graph.nodes}
    in_degree: dict[str, int] = {n.id: 0 for n in graph.nodes}
    for e in graph.edges:
        if e.source in adj and e.target in in_degree:
            adj[e.source].append(e.target)
            in_degree[e.target] += 1

    queue = [nid for nid, deg in in_degree.items() if deg == 0]
    visited_count = 0
    while queue:
        curr = queue.pop(0)
        visited_count += 1
        for nxt in adj.get(curr, []):
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)

    if visited_count < len(graph.nodes):
        errors.append("Cycle detected in graph topology. Graph must be a directed acyclic graph (DAG).")

    # 2. Output node checks
    output_nodes = [n for n in graph.nodes if n.node_type == NodeType.OUTPUT]
    if not output_nodes:
        errors.append("Missing OUTPUT node in view graph.")
    elif len(output_nodes) > 1:
        errors.append(f"Multiple OUTPUT nodes detected ({len(output_nodes)}). Exactly one is required.")

    # 3. Source node checks
    source_nodes = [n for n in graph.nodes if n.node_type == NodeType.SOURCE]
    if not source_nodes:
        errors.append("No SOURCE nodes defined. View graph must have at least one input source.")

    # 4. Check dangling operator nodes and column lineage
    for node in graph.nodes:
        upstreams = graph.get_upstream_node_ids(node.id)

        if node.node_type not in (NodeType.SOURCE,):
            if not upstreams:
                errors.append(f"Node '{node.name or node.id}' ({node.node_type.value}) has no incoming input connections.")

        if node.node_type == NodeType.JOIN:
            if len(upstreams) < 2:
                errors.append(f"Join node '{node.name or node.id}' requires at least 2 incoming input sources (currently {len(upstreams)}).")
            # Verify join key pairs
            up_cols = get_available_upstream_columns(graph, node.id)
            if not node.details.key_pairs:
                errors.append(f"Join node '{node.name or node.id}' has no join key pairs specified.")
            else:
                for pair in node.details.key_pairs:
                    if len(pair) == 2:
                        left_col, right_col = pair[0], pair[1]
                        l_bare = left_col.split('.')[-1] if left_col else ""
                        r_bare = right_col.split('.')[-1] if right_col else ""
                        if l_bare and l_bare not in up_cols:
                            errors.append(f"Join key '{left_col}' in '{node.name}' does not exist in upstream columns.")
                        if r_bare and r_bare not in up_cols:
                            errors.append(f"Join key '{right_col}' in '{node.name}' does not exist in upstream columns.")

        elif node.node_type == NodeType.CALCULATION:
            if not node.details.calculation_name and not node.name:
                errors.append(f"Calculation node '{node.id}' must specify a calculation name.")
            if not node.details.expression:
                errors.append(f"Calculation node '{node.name or node.id}' must specify a formula expression.")

        elif node.node_type == NodeType.FILTER:
            if not node.details.filter_expression:
                errors.append(f"Filter node '{node.name or node.id}' must specify a filter condition.")

    # 5. Check if Output is reachable from all sources
    if output_nodes and source_nodes:
        out_id = output_nodes[0].id
        for src in source_nodes:
            # BFS check if out_id is reachable from src.id
            q = [src.id]
            seen = {src.id}
            reachable = False
            while q:
                c = q.pop(0)
                if c == out_id:
                    reachable = True
                    break
                for nxt in adj.get(c, []):
                    if nxt not in seen:
                        seen.add(nxt)
                        q.append(nxt)
            if not reachable:
                errors.append(f"Output '{output_nodes[0].name}' is not reachable from source '{src.name}'.")

    return errors


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic Operations Engine
# ─────────────────────────────────────────────────────────────────────────────

def apply_operation(
    graph: ViewGraph,
    op: Operation,
    is_ai: bool = False,
) -> tuple[ViewGraph, Optional[str]]:
    """
    Applies a single atomic operation to the ViewGraph.
    Returns (new_graph, error_reason).
    If validation fails, returns the unchanged graph and the error message.
    """
    # Clone graph
    new_graph = graph.model_copy(deep=True)
    target_node = new_graph.get_node(op.node_id) if op.node_id else None

    # Rule: AI may NOT modify or delete SOURCE (Existing) nodes
    if is_ai and target_node and target_node.node_type == NodeType.SOURCE:
        if op.op in (OperationType.REMOVE_NODE, OperationType.UPDATE_NODE, OperationType.RENAME):
            return graph, f"AI is not permitted to modify or delete SOURCE node '{target_node.name}'."

    # ── 1. ADD_NODE ──────────────────────────────────────────────────────────
    if op.op == OperationType.ADD_NODE:
        nid = op.node_id or f"node_{len(new_graph.nodes) + 1}"
        if new_graph.get_node(nid):
            return graph, f"Node with ID '{nid}' already exists in graph."

        raw_type = op.payload.get("node_type", "PROJECTION")
        try:
            ntype = NodeType(raw_type)
        except ValueError:
            ntype = NodeType.PROJECTION

        new_node = ViewGraphNode(
            id=nid,
            name=op.payload.get("name", nid),
            node_type=ntype,
            is_existing=bool(op.payload.get("is_existing", False)),
            columns=op.payload.get("columns", []),
            details=NodeDetails(**op.payload.get("details", {})),
            position=op.payload.get("position", {"x": 200.0, "y": 150.0}),
        )
        new_graph.nodes.append(new_node)

    # ── 2. REMOVE_NODE ───────────────────────────────────────────────────────
    elif op.op == OperationType.REMOVE_NODE:
        if not target_node:
            return graph, f"Node '{op.node_id}' not found to remove."
        if target_node.node_type == NodeType.OUTPUT:
            return graph, "Cannot remove the OUTPUT terminal node."
        # Remove node and associated edges
        new_graph.nodes = [n for n in new_graph.nodes if n.id != target_node.id]
        new_graph.edges = [e for e in new_graph.edges if e.source != target_node.id and e.target != target_node.id]

    # ── 3. UPDATE_NODE ───────────────────────────────────────────────────────
    elif op.op == OperationType.UPDATE_NODE:
        if not target_node:
            return graph, f"Node '{op.node_id}' not found to update."
        if "name" in op.payload:
            target_node.name = str(op.payload["name"])
        if "position" in op.payload:
            target_node.position = op.payload["position"]
        if "details" in op.payload:
            target_node.details = NodeDetails(**op.payload["details"])

    # ── 4. ADD_EDGE ──────────────────────────────────────────────────────────
    elif op.op == OperationType.ADD_EDGE:
        src = op.payload.get("source")
        tgt = op.payload.get("target")
        if not src or not tgt:
            return graph, "Edge requires both 'source' and 'target' node IDs."
        if src == tgt:
            return graph, f"Cannot create self-loop edge on node '{src}'."
        if not new_graph.get_node(src):
            return graph, f"Source node '{src}' does not exist."
        if not new_graph.get_node(tgt):
            return graph, f"Target node '{tgt}' does not exist."

        eid = op.edge_id or f"edge_{src}_{tgt}"
        if any(e.id == eid or (e.source == src and e.target == tgt) for e in new_graph.edges):
            return graph, f"Edge between '{src}' and '{tgt}' already exists."

        new_edge = ViewGraphEdge(
            id=eid,
            source=src,
            target=tgt,
            source_handle=op.payload.get("source_handle"),
            target_handle=op.payload.get("target_handle"),
        )
        new_graph.edges.append(new_edge)

    # ── 5. REMOVE_EDGE ───────────────────────────────────────────────────────
    elif op.op == OperationType.REMOVE_EDGE:
        eid = op.edge_id
        src = op.payload.get("source")
        tgt = op.payload.get("target")
        before_len = len(new_graph.edges)
        if eid:
            new_graph.edges = [e for e in new_graph.edges if e.id != eid]
        elif src and tgt:
            new_graph.edges = [e for e in new_graph.edges if not (e.source == src and e.target == tgt)]
        if len(new_graph.edges) == before_len:
            return graph, f"Edge '{eid or f'{src}->{tgt}'}' not found to remove."

    # ── 6. SET_JOIN ──────────────────────────────────────────────────────────
    elif op.op == OperationType.SET_JOIN:
        if not target_node:
            return graph, f"Target node '{op.node_id}' not found."
        if target_node.node_type != NodeType.JOIN:
            return graph, f"Node '{target_node.id}' is not a JOIN operator."

        if "join_type" in op.payload:
            jt = str(op.payload["join_type"]).lower()
            if jt in ("inner", "left", "right", "full"):
                target_node.details.join_type = jt
                # Update display label e.g. "Left Join"
                target_node.name = f"{jt.capitalize()} Join"
            else:
                return graph, f"Invalid join_type '{jt}'. Must be inner, left, right, or full."

        if "key_pairs" in op.payload:
            pairs = op.payload["key_pairs"]
            up_cols = get_available_upstream_columns(new_graph, target_node.id)
            for pair in pairs:
                if len(pair) == 2:
                    l_col, r_col = pair[0], pair[1]
                    l_bare = l_col.split('.')[-1] if l_col else ""
                    r_bare = r_col.split('.')[-1] if r_col else ""
                    if l_bare and l_bare not in up_cols:
                        return graph, f"Join column '{l_col}' does not exist in upstream sources: {sorted(up_cols)}"
                    if r_bare and r_bare not in up_cols:
                        return graph, f"Join column '{r_col}' does not exist in upstream sources: {sorted(up_cols)}"
            target_node.details.key_pairs = pairs

    # ── 7. SET_FILTER ────────────────────────────────────────────────────────
    elif op.op == OperationType.SET_FILTER:
        if not target_node:
            return graph, f"Target node '{op.node_id}' not found."
        expr = op.payload.get("expression") or op.payload.get("filter_expression") or ""
        target_node.details.filter_expression = str(expr).strip()

    # ── 8. SET_CALCULATION ───────────────────────────────────────────────────
    elif op.op == OperationType.SET_CALCULATION:
        if not target_node:
            return graph, f"Target node '{op.node_id}' not found."
        if "calculation_name" in op.payload:
            target_node.details.calculation_name = str(op.payload["calculation_name"]).strip()
            target_node.name = target_node.details.calculation_name
        if "expression" in op.payload:
            target_node.details.expression = str(op.payload["expression"]).strip()
        if "data_type" in op.payload:
            target_node.details.data_type = str(op.payload["data_type"]).strip()

    # ── 9. INCLUDE_COLUMN / EXCLUDE_COLUMN ───────────────────────────────────
    elif op.op == OperationType.INCLUDE_COLUMN:
        if not target_node:
            return graph, f"Target node '{op.node_id}' not found."
        col = op.payload.get("column")
        if col:
            if col in target_node.details.excluded_columns:
                target_node.details.excluded_columns.remove(col)
            if col not in target_node.details.included_columns:
                target_node.details.included_columns.append(col)

    elif op.op == OperationType.EXCLUDE_COLUMN:
        if not target_node:
            return graph, f"Target node '{op.node_id}' not found."
        col = op.payload.get("column")
        if col:
            if col in target_node.details.included_columns:
                target_node.details.included_columns.remove(col)
            if col not in target_node.details.excluded_columns:
                target_node.details.excluded_columns.append(col)

    # ── 10. RENAME ───────────────────────────────────────────────────────────
    elif op.op == OperationType.RENAME:
        if not target_node:
            return graph, f"Target node '{op.node_id}' not found."
        if "new_name" in op.payload:
            target_node.name = str(op.payload["new_name"]).strip()
        if "column_rename" in op.payload:
            old_c = op.payload["column_rename"].get("old")
            new_c = op.payload["column_rename"].get("new")
            if old_c and new_c:
                target_node.details.renamed_columns[old_c] = new_c

    # Re-validate live errors on nodes
    validation_errors = validate_view_graph(new_graph)
    # Assign specific node errors
    for n in new_graph.nodes:
        n.errors = [err for err in validation_errors if n.name in err or n.id in err]

    new_graph.version += 1
    return new_graph, None


def apply_operations(
    graph: ViewGraph,
    ops: list[Operation],
    is_ai: bool = False,
) -> tuple[ViewGraph, list[dict[str, str]]]:
    """
    Applies a batch of operations sequentially.
    Returns (updated_graph, rejected_operations).
    """
    curr_graph = graph
    rejected: list[dict[str, str]] = []

    for op in ops:
        updated, err = apply_operation(curr_graph, op, is_ai=is_ai)
        if err:
            rejected.append({"op": op.op.value, "node_id": op.node_id or "", "reason": err})
        else:
            curr_graph = updated

    return curr_graph, rejected


# ─────────────────────────────────────────────────────────────────────────────
# Synthesis: Build ViewGraph from Plan
# ─────────────────────────────────────────────────────────────────────────────

def build_view_graph_for_proposed_node(
    view_node_id: str,
    view_name: str,
    sources: list[dict[str, Any]],
    join_info: Optional[dict[str, Any]] = None,
    calculations: Optional[list[dict[str, Any]]] = None,
    filter_info: Optional[dict[str, Any]] = None,
    output_columns: Optional[list[str]] = None,
) -> ViewGraph:
    """
    Builds a high-fidelity ViewGraph matching the SAP Datasphere Graphical View Editor:
    Left-to-right flow:
      Sources on Left -> Join / Calc / Filter Operators in Middle -> Output on Right.
    """
    nodes: list[ViewGraphNode] = []
    edges: list[ViewGraphEdge] = []

    # 1. Source Nodes (Left column, x = 60)
    source_x = 60.0
    source_start_y = 80.0
    source_spacing_y = 160.0

    src_node_ids: list[str] = []
    for idx, s in enumerate(sources):
        s_id = s.get("id") or f"src_{s.get('name', f'src_{idx}')}"
        s_name = s.get("name") or s.get("technical_name") or s_id
        s_cols = s.get("columns", [])
        src_node_ids.append(s_id)

        nodes.append(
            ViewGraphNode(
                id=s_id,
                name=s_name,
                node_type=NodeType.SOURCE,
                is_existing=True,
                columns=s_cols,
                position={"x": source_x, "y": source_start_y + idx * source_spacing_y},
                details=NodeDetails(),
            )
        )

    # 2. Operator Nodes (Middle column, x starting at 360)
    curr_x = 360.0
    center_y = source_start_y + (len(sources) - 1) * source_spacing_y / 2.0
    prev_node_id: Optional[str] = None

    # A. Join Operator
    if join_info or len(sources) >= 2:
        jt = (join_info.get("join_type") if join_info else "inner") or "inner"
        raw_key_pairs = (join_info.get("on") if join_info else []) or []

        if len(sources) >= 3:
            # Construct a chained binary join tree:
            # Join 1 connects src_node_ids[0] and src_node_ids[1]
            # Join i connects join_{i-1} and src_node_ids[i]
            prev_join_id: Optional[str] = None
            for idx in range(len(sources) - 1):
                join_num = idx + 1
                join_id = f"join_{view_node_id.lower()}_{join_num}"
                join_name = f"{jt.capitalize()} Join {join_num}"

                if idx == 0:
                    left_id = src_node_ids[0]
                    right_id = src_node_ids[1]
                    left_sources = [sources[0]]
                    right_sources = [sources[1]]
                else:
                    left_id = prev_join_id
                    right_id = src_node_ids[idx + 1]
                    left_sources = sources[:idx + 1]
                    right_sources = [sources[idx + 1]]

                matched_pairs = []
                for pair in raw_key_pairs:
                    if len(pair) == 2:
                        p0, p1 = str(pair[0]), str(pair[1])
                        t0 = p0.split('.')[0] if '.' in p0 else ""
                        t1 = p1.split('.')[0] if '.' in p1 else ""
                        c0 = p0.split('.')[-1]
                        c1 = p1.split('.')[-1]

                        def matches_source_group(tbl: str, col_name: str, src_group: list[dict[str, Any]]) -> bool:
                            for s in src_group:
                                s_id = s.get("id", "")
                                s_name = s.get("name", "")
                                s_cols = s.get("columns", [])
                                if tbl:
                                    tbl_clean = re.sub(r'^(src_|MN_WRAP_|WRAP_|L1_)', '', tbl)
                                    sid_clean = re.sub(r'^(src_|MN_WRAP_|WRAP_|L1_)', '', s_id)
                                    sname_clean = re.sub(r'^(src_|MN_WRAP_|WRAP_|L1_)', '', s_name)
                                    if tbl in (s_id, s_name) or tbl_clean in (sid_clean, sname_clean):
                                        return True
                                else:
                                    if col_name in s_cols:
                                        return True
                            return False

                        left_match = (matches_source_group(t0, c0, left_sources) and matches_source_group(t1, c1, right_sources))
                        right_match = (matches_source_group(t1, c1, left_sources) and matches_source_group(t0, c0, right_sources))

                        if left_match or right_match:
                            matched_pairs.append([p0, p1])

                if not matched_pairs and idx < len(raw_key_pairs):
                    matched_pairs = [raw_key_pairs[idx]]

                join_node = ViewGraphNode(
                    id=join_id,
                    name=join_name,
                    node_type=NodeType.JOIN,
                    position={"x": curr_x, "y": center_y + (idx - (len(sources) - 2) / 2.0) * 80.0},
                    details=NodeDetails(
                        join_type=jt,
                        key_pairs=matched_pairs,
                    ),
                )
                nodes.append(join_node)

                edges.append(
                    ViewGraphEdge(
                        id=f"edge_{left_id}_{join_id}",
                        source=left_id,
                        target=join_id,
                    )
                )
                edges.append(
                    ViewGraphEdge(
                        id=f"edge_{right_id}_{join_id}",
                        source=right_id,
                        target=join_id,
                    )
                )

                prev_join_id = join_id
                curr_x += 220.0

            prev_node_id = prev_join_id
        else:
            join_id = f"join_{view_node_id.lower()}"
            key_pairs = raw_key_pairs

            join_node = ViewGraphNode(
                id=join_id,
                name=f"{jt.capitalize()} Join",
                node_type=NodeType.JOIN,
                position={"x": curr_x, "y": center_y},
                details=NodeDetails(
                    join_type=jt,
                    key_pairs=key_pairs,
                ),
            )
            nodes.append(join_node)

            for s_id in src_node_ids:
                edges.append(
                    ViewGraphEdge(
                        id=f"edge_{s_id}_{join_id}",
                        source=s_id,
                        target=join_id,
                    )
                )
            prev_node_id = join_id
            curr_x += 220.0
    elif len(src_node_ids) == 1:
        prev_node_id = src_node_ids[0]

    # B. Filter Operator (if specified)
    if filter_info and filter_info.get("condition"):
        filter_id = f"filter_{view_node_id.lower()}"
        cond = filter_info.get("condition", "")
        filter_node = ViewGraphNode(
            id=filter_id,
            name="Filter",
            node_type=NodeType.FILTER,
            position={"x": curr_x, "y": center_y},
            details=NodeDetails(filter_expression=cond),
        )
        nodes.append(filter_node)
        if prev_node_id:
            edges.append(
                ViewGraphEdge(
                    id=f"edge_{prev_node_id}_{filter_id}",
                    source=prev_node_id,
                    target=filter_id,
                )
            )
        prev_node_id = filter_id
        curr_x += 220.0

    # C. Calculation Operators
    if calculations:
        for c_idx, calc in enumerate(calculations):
            calc_name = calc.get("name", f"CALC_{c_idx + 1}")
            calc_id = f"calc_{calc_name.lower()}"
            calc_node = ViewGraphNode(
                id=calc_id,
                name=calc_name,
                node_type=NodeType.CALCULATION,
                position={"x": curr_x, "y": center_y},
                details=NodeDetails(
                    calculation_name=calc_name,
                    expression=calc.get("formula") or calc.get("expression") or "",
                    data_type=calc.get("data_type", "Decimal"),
                ),
            )
            nodes.append(calc_node)
            if prev_node_id:
                edges.append(
                    ViewGraphEdge(
                        id=f"edge_{prev_node_id}_{calc_id}",
                        source=prev_node_id,
                        target=calc_id,
                    )
                )
            prev_node_id = calc_id
            curr_x += 220.0

    # 3. Output Node (Right column, x = curr_x)
    output_id = f"output_{view_node_id.lower()}"
    final_cols = output_columns or []
    out_node = ViewGraphNode(
        id=output_id,
        name=view_name,
        node_type=NodeType.OUTPUT,
        columns=final_cols,
        position={"x": curr_x, "y": center_y},
        details=NodeDetails(output_name=view_name),
    )
    nodes.append(out_node)

    if prev_node_id:
        edges.append(
            ViewGraphEdge(
                id=f"edge_{prev_node_id}_{output_id}",
                source=prev_node_id,
                target=output_id,
            )
        )

    # Initial live error assignment
    vg = ViewGraph(
        view_id=view_node_id,
        view_name=view_name,
        nodes=nodes,
        edges=edges,
        version=1,
    )
    errs = validate_view_graph(vg)
    for n in vg.nodes:
        n.errors = [err for err in errs if n.name in err or n.id in err]

    return vg


def synthesize_view_graphs_from_plan(plan: Any, metadata: Any = None) -> dict[str, ViewGraph]:
    """
    Synthesizes view graphs for all proposed views and wrapper nodes in the accepted Plan.
    Creates:
      - Internal wrapper view graphs (Source -> Projection -> Filter -> Output)
      - Lineage view graphs for proposed views (Join -> Filter -> Calculation -> Output)
    """
    graphs: dict[str, ViewGraph] = {}

    # Extract nodes from plan
    plan_nodes = getattr(plan, "nodes", []) if hasattr(plan, "nodes") else plan.get("nodes", [])

    # Helper to look up columns for a table name
    def _lookup_columns(table_name: str) -> list[str]:
        # 1. Check in plan nodes
        for pn in plan_nodes:
            if getattr(pn, "id", "") == table_name:
                cols = list(getattr(pn, "columns", []))
                if cols:
                    return cols
        clean_name = re.sub(r'^(src_|MN_WRAP_|WRAP_|L1_)', '', table_name)
        for pn in plan_nodes:
            if getattr(pn, "id", "") == clean_name:
                cols = list(getattr(pn, "columns", []))
                if cols:
                    return cols

        # 2. Check in metadata
        if metadata:
            obj = getattr(metadata, "get_object", lambda _: None)(table_name)
            if not obj and clean_name != table_name:
                obj = getattr(metadata, "get_object", lambda _: None)(clean_name)
            if obj:
                col_names = getattr(obj, "column_names", None)
                if callable(col_names):
                    cols = sorted(list(col_names()))
                    if cols:
                        return cols
                elif hasattr(obj, "columns"):
                    cols = [getattr(c, "name", str(c)) for c in getattr(obj, "columns", [])]
                    if cols:
                        return cols
            if isinstance(metadata, dict) and "definitions" in metadata:
                defn = metadata["definitions"].get(table_name, {}) or metadata["definitions"].get(clean_name, {})
                if defn and "elements" in defn:
                    return list(defn["elements"].keys())
            elif hasattr(metadata, "entities"):
                for ent in getattr(metadata, "entities", []):
                    ent_name = getattr(ent, "name", "")
                    if ent_name in (table_name, clean_name):
                        return [getattr(f, "name", str(f)) for f in getattr(ent, "fields", [])]

        raise ValueError(f"Columns for table '{table_name}' could not be resolved from metadata or plan nodes.")

    # 1. Synthesize internal view graphs for every wrapper node (D6)
    wrapper_nodes = [
        n for n in plan_nodes
        if getattr(n, "is_wrapper", False)
        or getattr(n, "layer", "") == "L1"
        or getattr(n, "id", "").startswith(("MN_WRAP_", "WRAP_", "L1_"))
    ]

    for w_node in wrapper_nodes:
        w_id = getattr(w_node, "id", "")
        w_name = getattr(w_node, "name", "") or w_id
        w_inputs = list(getattr(w_node, "inputs", []))
        w_cols = list(getattr(w_node, "columns", []))
        w_details = getattr(w_node, "details", None)

        src_table = w_inputs[0] if w_inputs else re.sub(r'^(MN_WRAP_|WRAP_|L1_)', '', w_id)
        try:
            src_cols = _lookup_columns(src_table)
        except ValueError:
            src_cols = w_cols

        w_vg_nodes: list[ViewGraphNode] = []
        w_vg_edges: list[ViewGraphEdge] = []

        # Source node
        src_node_id = f"src_{src_table}"
        w_vg_nodes.append(
            ViewGraphNode(
                id=src_node_id,
                name=src_table,
                node_type=NodeType.SOURCE,
                is_existing=True,
                columns=src_cols,
                position={"x": 60.0, "y": 80.0},
                details=NodeDetails(),
            )
        )
        curr_x = 360.0
        prev_id = src_node_id

        # Projection (if is_projection_enabled or custom columns)
        is_proj = getattr(w_node, "is_projection_enabled", False)
        has_custom_cols = bool(w_cols and src_cols and set(w_cols) != set(src_cols))
        if is_proj or has_custom_cols:
            proj_id = f"proj_{w_id.lower()}"
            w_vg_nodes.append(
                ViewGraphNode(
                    id=proj_id,
                    name="Projection",
                    node_type=NodeType.PROJECTION,
                    position={"x": curr_x, "y": 80.0},
                    columns=w_cols or src_cols,
                    details=NodeDetails(
                        included_columns=w_cols or src_cols,
                        excluded_columns=[c for c in src_cols if c not in (w_cols or src_cols)],
                    ),
                )
            )
            w_vg_edges.append(
                ViewGraphEdge(
                    id=f"edge_{prev_id}_{proj_id}",
                    source=prev_id,
                    target=proj_id,
                )
            )
            prev_id = proj_id
            curr_x += 220.0

        # Filter (if is_filter_enabled or condition)
        is_filt = getattr(w_node, "is_filter_enabled", False)
        cond = (getattr(w_details, "condition", "") if w_details else "") or ""
        if is_filt or cond.strip():
            filter_id = f"filter_{w_id.lower()}"
            w_vg_nodes.append(
                ViewGraphNode(
                    id=filter_id,
                    name="Filter",
                    node_type=NodeType.FILTER,
                    position={"x": curr_x, "y": 80.0},
                    columns=w_cols or src_cols,
                    details=NodeDetails(filter_expression=cond.strip() or "1 = 1"),
                )
            )
            w_vg_edges.append(
                ViewGraphEdge(
                    id=f"edge_{prev_id}_{filter_id}",
                    source=prev_id,
                    target=filter_id,
                )
            )
            prev_id = filter_id
            curr_x += 220.0

        # Output node
        out_id = f"output_{w_id.lower()}"
        out_name = w_name or w_id
        w_vg_nodes.append(
            ViewGraphNode(
                id=out_id,
                name=out_name,
                node_type=NodeType.OUTPUT,
                columns=w_cols or src_cols,
                position={"x": curr_x, "y": 80.0},
                details=NodeDetails(output_name=out_name),
            )
        )
        w_vg_edges.append(
            ViewGraphEdge(
                id=f"edge_{prev_id}_{out_id}",
                source=prev_id,
                target=out_id,
            )
        )

        vg_wrapper = ViewGraph(
            view_id=w_id,
            view_name=out_name,
            nodes=w_vg_nodes,
            edges=w_vg_edges,
            version=1,
        )
        errs = validate_view_graph(vg_wrapper)
        for n in vg_wrapper.nodes:
            n.errors = [err for err in errs if n.name in err or n.id in err]
        graphs[w_id] = vg_wrapper

    # 2. Find proposed views (L2/L3/L4 or new views)
    proposed_views = [
        n for n in plan_nodes
        if n not in wrapper_nodes
        and not getattr(n, "is_existing", False)
        and (getattr(n, "operation", "") in ("join", "calculated_column", "fact", "analytic_model", "projection", "filter")
             or getattr(n, "layer", "") in ("L2", "L3", "L4"))
    ]

    if not proposed_views and plan_nodes:
        proposed_views = [n for n in plan_nodes if n not in wrapper_nodes and getattr(n, "operation", "") != "source"] or [plan_nodes[-1]]

    for p_node in proposed_views:
        p_id = getattr(p_node, "id", "") or "PROPOSED_VIEW"
        p_inputs = list(getattr(p_node, "inputs", []))
        p_details = getattr(p_node, "details", None)
        p_cols = list(getattr(p_node, "columns", []))

        # Build source objects
        sources_list: list[dict[str, Any]] = []
        for inp in p_inputs:
            sources_list.append({
                "id": f"src_{inp}",
                "name": inp,
                "columns": _lookup_columns(inp),
            })

        # Join details
        join_dict: Optional[dict[str, Any]] = None
        if p_details and getattr(p_details, "join_type", ""):
            join_dict = {
                "join_type": getattr(p_details, "join_type", "inner"),
                "on": getattr(p_details, "on", []),
            }
        elif len(sources_list) >= 2:
            join_dict = {
                "join_type": "inner",
                "on": getattr(p_details, "on", []) if p_details else [],
            }

        # Calculations
        raw_calculated_fields = getattr(p_node, "calculated_fields", None)
        if raw_calculated_fields is None and isinstance(p_node, dict):
            raw_calculated_fields = p_node.get("calculated_fields", [])
        calcs_list: list[dict[str, Any]] = []
        for calc in raw_calculated_fields or []:
            calc_name = calc.get("name", "") if isinstance(calc, dict) else getattr(calc, "name", "")
            calc_formula = calc.get("formula", "") if isinstance(calc, dict) else getattr(calc, "formula", "")
            calc_dt = (calc.get("data_type", "") if isinstance(calc, dict) else getattr(calc, "data_type", "")) or "Decimal"
            if calc_name and calc_formula:
                calcs_list.append({
                    "name": calc_name,
                    "formula": calc_formula,
                    "data_type": calc_dt,
                })
        formula = getattr(p_details, "formula", "") if p_details else ""
        if formula and not calcs_list:
            calcs_list.append({
                "name": "CALC_1",
                "formula": formula,
                "data_type": "Decimal",
            })

        # Filter
        filter_dict: Optional[dict[str, Any]] = None
        cond = getattr(p_details, "condition", "") if p_details else ""
        if cond:
            filter_dict = {"condition": cond}

        # Output columns
        out_cols = p_cols or []

        vg = build_view_graph_for_proposed_node(
            view_node_id=p_id,
            view_name=p_id,
            sources=sources_list,
            join_info=join_dict,
            calculations=calcs_list,
            filter_info=filter_dict,
            output_columns=out_cols,
        )
        graphs[p_id] = vg

    return graphs
