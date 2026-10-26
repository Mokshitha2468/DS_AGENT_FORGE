"""
Deterministic Plan Checker.

Enforces modeling integrity rules on the generated Plan:
1. Tables and existing views referenced in inputs exist in metadata.
2. Step inputs exist (produced by metadata or earlier nodes).
3. No cycles in the DAG (strict topological order).
4. Join keys exist on the left and right inputs.
5. No hallucinated columns (every column must trace back to metadata or a calculated column).
6. Plan measures and dimensions exist in the produced views.
"""
from __future__ import annotations

import re
from typing import Any
from pydantic import BaseModel, Field

from app.agent.plan_models import Plan, PlanNode, ALLOWED_OPERATIONS
from app.agent.cds_extractor import ExtractedMetadata


class PlanCheckResult(BaseModel):
    is_valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def check_plan(plan: Plan, metadata: Optional[ExtractedMetadata] = None) -> PlanCheckResult:
    """
    Validates a generated Plan against the extracted source metadata.
    Returns PlanCheckResult with is_valid=True if all checks pass.
    """
    errors: list[str] = []
    warnings: list[str] = []

    # Map of object/node name -> set of available column names
    available_objects: dict[str, set[str]] = {}
    if metadata and hasattr(metadata, "objects"):
        for name, obj in metadata.objects.items():
            available_objects[name] = obj.column_names()

    # 1. Check for duplicate node IDs
    seen_node_ids: set[str] = set()
    for node in plan.nodes:
        if node.id in seen_node_ids:
            errors.append(f"Duplicate node ID '{node.id}' found in plan.")
        seen_node_ids.add(node.id)

    # 2. Check for cycles in step dependencies
    cycle = _detect_cycle(plan.nodes)
    if cycle:
        errors.append(f"Cycle detected in plan dependencies: {' -> '.join(cycle)}")

    # Map of plan nodes for qualifier matching
    plan_nodes = {n.id: n for n in plan.nodes}

    # 3. Step-by-step validation
    for node in plan.nodes:
        # Check operation validity
        if node.operation not in ALLOWED_OPERATIONS:
            errors.append(
                f"Node '{node.id}' uses invalid operation '{node.operation}'. "
                f"Allowed operations are: {', '.join(sorted(ALLOWED_OPERATIONS))}"
            )

        # Check step inputs exist
        input_columns: set[str] = set()
        for inp in node.inputs:
            if inp not in available_objects:
                errors.append(
                    f"Node '{node.id}' references input '{inp}' which does not exist "
                    f"in metadata or preceding steps."
                )
            else:
                input_columns.update(available_objects[inp])

        # If node has no inputs, resolve from metadata source object (e.g. L0_VBAK -> VBAK)
        if not node.inputs and metadata:
            clean_id = re.sub(r'^(L0_|L1_|SRC_|WRAP_|MN_WRAP_)', '', node.id)
            for candidate in (node.id, clean_id):
                if candidate in available_objects:
                    input_columns.update(available_objects[candidate])
                    break

        # If operation is a join, validate join keys
        if node.operation == "join":
            _validate_join_node(node, available_objects, plan_nodes, errors, warnings)

        # Calculate this node's output columns
        output_cols = _compute_node_columns(node, input_columns, metadata, errors)
        available_objects[node.id] = output_cols

    # 4. Topology and Layer Rules (1e)
    fact_nodes = [n for n in plan.nodes if n.layer == "L3" or n.operation == "fact"]
    if not fact_nodes:
        fact_nodes = [n for n in plan.nodes if n.operation == "join" and n.layer not in ("L1", "L2")]

    fact_node: PlanNode | None = None
    if len(fact_nodes) != 1:
        if not plan.blockers:
            errors.append(f"Plan must contain exactly one L3 fact node, got {len(fact_nodes)}.")
    else:
        fact_node = fact_nodes[0]

    model_nodes = [n for n in plan.nodes if n.operation == "analytic_model" or n.layer == "L4"]
    if len(model_nodes) != 1:
        if not plan.blockers:
            errors.append(f"Plan must contain exactly one analytic_model node, got {len(model_nodes)}.")
    else:
        model_node = model_nodes[0]
        if fact_node and model_node.inputs != [fact_node.id]:
            errors.append(
                f"Analytic model node '{model_node.id}' input must be the fact node '{fact_node.id}', got {model_node.inputs}."
            )

        measure_names = {m.name for m in plan.measures}
        dim_set = {_strip_qualifier(d) for d in plan.dimensions}
        for col in model_node.columns:
            clean_col = _strip_qualifier(col)
            if clean_col in measure_names:
                errors.append(
                    f"Analytic model '{model_node.id}' column '{col}' is a measure name; "
                    "analytic model columns must contain dimension columns only."
                )
            elif clean_col not in dim_set:
                errors.append(
                    f"Analytic model '{model_node.id}' column '{col}' is not a defined dimension; "
                    "analytic model columns must contain dimension columns only."
                )

    if fact_node:
        fact_cols = available_objects.get(fact_node.id, set())
        for dim in plan.dimensions:
            clean_dim = _strip_qualifier(dim)
            if clean_dim not in fact_cols:
                errors.append(
                    f"Dimension '{dim}' not found in fact node '{fact_node.id}' output columns."
                )

    # 5. Validate Measures (1a, 1b, 1c)
    all_measure_names = {m.name for m in plan.measures}
    for m in plan.measures:
        agg = m.aggregation.upper()
        if agg == "FORMULA":
            if not m.formula or not m.formula.strip():
                errors.append(f"Measure '{m.name}' has aggregation FORMULA but formula is empty.")
            else:
                raw_tokens = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", m.formula)
                sql_keywords = {
                    "CASE", "WHEN", "THEN", "ELSE", "END", "OR", "AND", "NOT",
                    "IS", "NULL", "TRUE", "FALSE"
                }
                other_measure_names = {name for name in all_measure_names if name != m.name}
                for token in raw_tokens:
                    if token.upper() in sql_keywords:
                        continue
                    if token not in other_measure_names:
                        errors.append(
                            f"Measure '{m.name}' formula references '{token}' which is not a defined measure name in the plan."
                        )
        else:
            if re.search(r"[\s+\-*/%<>=()]", m.column):
                errors.append(
                    f"Measure '{m.name}' contains operators or spaces in column '{m.column}': "
                    "Move the expression to `formula` and set aggregation to FORMULA."
                )
            else:
                clean_col = _strip_qualifier(m.column)
                col_found = any(clean_col in cols for cols in available_objects.values())
                if not col_found and clean_col not in ("*", "1"):
                    errors.append(
                        f"Measure '{m.name}' references column '{m.column}' which does not exist in any view."
                    )

    # 6. Informational: Blockers
    if plan.blockers:
        warnings.append(f"Plan identified {len(plan.blockers)} blocker(s): {'; '.join(plan.blockers)}")

    return PlanCheckResult(
        is_valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
    )


def _validate_join_node(
    node: PlanNode,
    available_objects: dict[str, set[str]],
    plan_nodes: dict[str, PlanNode],
    errors: list[str],
    warnings: list[str],
) -> None:
    """Validates each join key without assuming positional indices."""
    if len(node.inputs) < 2:
        errors.append(f"Join node '{node.id}' must specify at least 2 inputs, got {len(node.inputs)}.")
        return

    join_on = node.details.on
    if not join_on:
        errors.append(f"Join node '{node.id}' missing join condition ('on' keys).")
        return

    resolved_pairs: list[tuple[str, str]] = []

    for pair in join_on:
        if len(pair) != 2:
            errors.append(f"Join node '{node.id}' join condition must be [left_col, right_col] pair, got {pair}.")
            continue

        pair_inputs: list[str | None] = []
        for ref in (pair[0], pair[1]):
            input_name = _resolve_join_key_input(ref, node.inputs, available_objects, plan_nodes, node.id, errors)
            pair_inputs.append(input_name)

        if pair_inputs[0] is not None and pair_inputs[1] is not None:
            if pair_inputs[0] == pair_inputs[1]:
                errors.append(
                    f"Join key pair {pair} in node '{node.id}' connects the same input '{pair_inputs[0]}'; "
                    "each key pair must connect two different inputs."
                )
            else:
                resolved_pairs.append((pair_inputs[0], pair_inputs[1]))

    for i in range(1, len(node.inputs)):
        inp = node.inputs[i]
        prior_inputs = set(node.inputs[:i])
        connected = any(
            (l == inp and r in prior_inputs) or (r == inp and l in prior_inputs)
            for l, r in resolved_pairs
        )
        if not connected:
            errors.append(f"Join node '{node.id}': input '{inp}' is not joined.")


def _resolve_join_key_input(
    ref: str,
    inputs: list[str],
    available_objects: dict[str, set[str]],
    plan_nodes: dict[str, PlanNode],
    node_id: str,
    errors: list[str],
) -> str | None:
    """Resolves a qualified or bare join key to its matching input."""
    if "." in ref:
        qualifier, col = ref.rsplit(".", 1)
        col = col.strip()
        matched_input = None
        if qualifier in inputs:
            matched_input = qualifier
        else:
            for inp in inputs:
                inp_node = plan_nodes.get(inp)
                if inp_node and qualifier in inp_node.inputs:
                    matched_input = inp
                    break
                for prefix in ("MN_WRAP_", "WRAP_", "L1_"):
                    if inp.startswith(prefix) and inp[len(prefix):] == qualifier:
                        matched_input = inp
                        break
                if matched_input:
                    break

        if not matched_input:
            errors.append(
                f"Join node '{node_id}' join key '{ref}' references input '{qualifier}', "
                f"which is not one of the join inputs: {inputs}."
            )
            return None

        cols = available_objects.get(matched_input, set())
        if col not in cols:
            errors.append(
                f"Join node '{node_id}' join key '{ref}' not found in input '{matched_input}'. "
                f"Available columns: {sorted(cols)}"
            )
            return None
        return matched_input
    else:
        bare_col = ref.strip()
        matching_inputs = [inp for inp in inputs if bare_col in available_objects.get(inp, set())]
        if len(matching_inputs) == 1:
            return matching_inputs[0]
        elif len(matching_inputs) > 1:
            errors.append(
                f"Join key '{ref}' in node '{node_id}' is ambiguous across inputs {matching_inputs}; "
                "qualify with the input name."
            )
            return None
        else:
            errors.append(
                f"Join key '{ref}' in node '{node_id}' not found in any input: {inputs}."
            )
            return None


def _compute_node_columns(
    node: PlanNode,
    input_columns: set[str],
    metadata: ExtractedMetadata,
    errors: list[str],
) -> set[str]:
    """Computes output columns for a node and flags hallucinated columns."""
    output_cols: set[str] = set()

    if node.columns:
        for col in node.columns:
            output_cols.add(_strip_qualifier(col))
    for calculated_field in node.calculated_fields:
        if not calculated_field.name.strip() or not calculated_field.formula.strip():
            errors.append(
                f"Node '{node.id}' has a calculated field that requires both a name and formula."
            )
        else:
            output_cols.add(_strip_qualifier(calculated_field.name))

    # If node is an existing CDS view in metadata, its allowed columns come from its definition
    clean_node_id = re.sub(r'^(L0_|L1_|SRC_|WRAP_|MN_WRAP_)', '', node.id)
    existing_obj = None
    if metadata:
        if node.is_existing or not node.inputs:
            existing_obj = metadata.get_object(node.id) or metadata.get_object(clean_node_id)
        elif not input_columns and clean_node_id in getattr(metadata, "objects", {}):
            existing_obj = metadata.get_object(clean_node_id)
    existing_cols = existing_obj.column_names() if existing_obj else set()

    # If node introduces a calculated column, include it
    if node.operation == "calculated_column" or node.details.formula or node.calculated_fields:
        if not node.columns and node.id:
            output_cols.add(node.id)

    # If no explicit columns were given, inherit input columns
    if not output_cols and input_columns:
        output_cols = set(input_columns)

    # Check for hallucinated columns
    if node.operation in ("projection", "filter") and node.columns:
        allowed = input_columns.union(existing_cols)
        for col in node.columns:
            clean_col = _strip_qualifier(col)
            if clean_col not in allowed:
                errors.append(
                    f"Node '{node.id}' ({node.operation}) selects column '{col}' which does not exist "
                    f"in its input(s) or existing metadata. Available: {sorted(allowed)}"
                )

    return output_cols


def _strip_qualifier(col_ref: str) -> str:
    """Strips table alias qualifier: 'VBAK.VBELN' -> 'VBELN'."""
    return col_ref.split(".")[-1].strip()


def _detect_cycle(nodes: list[PlanNode]) -> list[str] | None:
    """Detects cycles in node dependencies using DFS."""
    adj: dict[str, list[str]] = {n.id: [] for n in nodes}
    for n in nodes:
        for inp in n.inputs:
            if inp in adj:
                adj[inp].append(n.id)

    visited: dict[str, int] = {n.id: 0 for n in nodes}  # 0=unvisited, 1=visiting, 2=visited
    cycle_path: list[str] = []

    def dfs(curr: str, path: list[str]) -> bool:
        visited[curr] = 1
        path.append(curr)

        for neighbor in adj.get(curr, []):
            if visited.get(neighbor) == 1:
                cycle_path.extend(path[path.index(neighbor):] + [neighbor])
                return True
            if visited.get(neighbor) == 0:
                if dfs(neighbor, path):
                    return True

        path.pop()
        visited[curr] = 2
        return False

    for n in nodes:
        if visited[n.id] == 0:
            if dfs(n.id, []):
                return cycle_path

    return None


def validate_plan_for_confirm(plan: Plan, source_entities: Any = None) -> PlanCheckResult:
    """
    Validation gate for plan confirmation before export / lineage synthesis.
    Enforces that all wrappers, fact joins, calculated fields, measures, dimensions,
    and filter conditions are fully consistent and provide all referenced columns.
    """
    errors: list[str] = []
    warnings: list[str] = []

    sql_keywords = {
        "CASE", "WHEN", "THEN", "ELSE", "END", "OR", "AND", "NOT",
        "IS", "NULL", "TRUE", "FALSE"
    }

    # 1. Delta entities (_DELTA, -DELTA)
    for node in plan.nodes:
        if node.id.upper().endswith(("_DELTA", "-DELTA")):
            errors.append(f"Node '{node.id}' references forbidden delta replication entity.")
        for inp in node.inputs:
            if inp.upper().endswith(("_DELTA", "-DELTA")):
                errors.append(f"Node '{node.id}' references input '{inp}' which is a forbidden delta replication entity.")

    # 2. Not exactly one fact / analytic_model
    fact_nodes = [n for n in plan.nodes if n.layer == "L3" or n.operation in ("fact", "join") and n.layer not in ("L1", "L2")]
    if len(fact_nodes) != 1:
        errors.append(f"Plan must contain exactly one fact node, got {len(fact_nodes)}.")
        fact_node = None
    else:
        fact_node = fact_nodes[0]

    model_nodes = [n for n in plan.nodes if n.operation == "analytic_model" or n.layer == "L4"]
    if len(model_nodes) != 1:
        errors.append(f"Plan must contain exactly one analytic_model node, got {len(model_nodes)}.")
        model_node = None
    else:
        model_node = model_nodes[0]

    # 3. Fact input that is not a wrapper
    if fact_node:
        for inp in fact_node.inputs:
            inp_node = next((n for n in plan.nodes if n.id == inp), None)
            is_wrap = (
                (inp_node is not None and inp_node.is_wrapper)
                or inp.startswith(("MN_WRAP_", "WRAP_", "L1_"))
            )
            if not is_wrap:
                errors.append(f"Fact node '{fact_node.id}' input '{inp}' is not a wrapper.")

    # 4. Source with zero or more than one wrapper (for sources used in the plan)
    source_names: set[str] = set()
    for n in plan.nodes:
        if n.operation == "source" or n.layer == "L0":
            source_names.add(n.id)
        if n.is_wrapper or n.id.startswith(("MN_WRAP_", "WRAP_", "L1_")):
            for inp in n.inputs:
                if not inp.startswith(("MN_WRAP_", "WRAP_", "L1_")):
                    source_names.add(inp)

    # Exclude delta sources
    source_names = {s for s in source_names if not s.upper().endswith(("_DELTA", "-DELTA"))}

    # Verify sources exist in source_entities if provided
    if source_entities is not None:
        known_sources: set[str] = set()
        if hasattr(source_entities, "objects"):
            known_sources = set(source_entities.objects.keys())
        elif isinstance(source_entities, list):
            for e in source_entities:
                if isinstance(e, dict) and "name" in e:
                    known_sources.add(e["name"])
                elif hasattr(e, "name"):
                    known_sources.add(e.name)
        if known_sources:
            for s in source_names:
                if s not in known_sources:
                    errors.append(f"Source entity '{s}' does not exist in metadata.")

    for source in sorted(source_names):
        wrappers_for_source = [
            n for n in plan.nodes
            if (n.is_wrapper or n.id.startswith(("MN_WRAP_", "WRAP_", "L1_")))
            and (source in n.inputs or n.id in (f"MN_WRAP_{source}", f"WRAP_{source}", f"L1_{source}"))
        ]
        if len(wrappers_for_source) == 0:
            errors.append(f"Source '{source}' has 0 wrappers; expected exactly one wrapper.")
        elif len(wrappers_for_source) > 1:
            errors.append(f"Source '{source}' has {len(wrappers_for_source)} wrappers; expected exactly one wrapper.")

    # 5. Wrapper with filter enabled and an empty condition
    for node in plan.nodes:
        is_wrap = node.is_wrapper or node.id.startswith(("MN_WRAP_", "WRAP_", "L1_"))
        if is_wrap and node.is_filter_enabled:
            if not node.details.condition or not node.details.condition.strip():
                errors.append(f"Wrapper '{node.id}' has filter enabled but condition is empty.")

    # 6. Column existence checks
    # A) Wrapper filter identifier
    for node in plan.nodes:
        is_wrap = node.is_wrapper or node.id.startswith(("MN_WRAP_", "WRAP_", "L1_"))
        if is_wrap and node.details.condition and node.details.condition.strip():
            raw_tokens = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", node.details.condition)
            func_names = set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", node.details.condition))
            wrap_cols = set(node.columns)
            for token in raw_tokens:
                if token.upper() in sql_keywords or token in func_names:
                    continue
                if token not in wrap_cols:
                    errors.append(f"Wrapper '{node.id}' filter condition references column '{token}' which is missing from wrapper.")

    # B) Join keys on the fact node
    if fact_node and fact_node.details.on:
        for pair in fact_node.details.on:
            for reference in pair:
                col = reference.split('.')[-1].strip()
                qualifier = reference.split('.')[0].strip() if '.' in reference else None
                if qualifier:
                    inp_node = next((n for n in plan.nodes if n.id == qualifier or (n.is_wrapper and qualifier in n.inputs)), None)
                    if inp_node:
                        if col not in inp_node.columns:
                            errors.append(f"Fact node '{fact_node.id}' join key '{reference}' column '{col}' is missing from input '{inp_node.id}'.")
                else:
                    found_in_wrap = any(col in n.columns for n in plan.nodes if n.id in fact_node.inputs)
                    if not found_in_wrap:
                        errors.append(f"Fact node '{fact_node.id}' join key '{reference}' column '{col}' is missing from fact wrapper inputs.")

    # C) Formula identifiers in calculated fields
    if fact_node:
        calc_output_names = {cf.name for cf in fact_node.calculated_fields}
        fact_cols = set(fact_node.columns).union(calc_output_names)
        for cf in fact_node.calculated_fields:
            if cf.formula:
                tokens = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", cf.formula)
                func_names = set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", cf.formula))
                for token in tokens:
                    if token.upper() in sql_keywords or token in func_names:
                        continue
                    if token not in fact_cols and token not in calc_output_names:
                        in_wrapper = any(token in n.columns for n in plan.nodes if n.id in fact_node.inputs)
                        if not in_wrapper:
                            errors.append(f"Fact node '{fact_node.id}' calculated field '{cf.name}' formula references column '{token}' which is missing.")

    # D) Measure column
    if fact_node:
        fact_cols = set(fact_node.columns).union({cf.name for cf in fact_node.calculated_fields})
        for m in plan.measures:
            if m.aggregation.upper() != "FORMULA" and m.column and m.column not in ("*", "1"):
                clean_col = _strip_qualifier(m.column)
                if clean_col not in fact_cols:
                    errors.append(f"Measure '{m.name}' references column '{m.column}' which is missing from fact node '{fact_node.id}'.")

    # E) Dimension
    if fact_node:
        fact_cols = set(fact_node.columns).union({cf.name for cf in fact_node.calculated_fields})
        for dim in plan.dimensions:
            clean_dim = _strip_qualifier(dim)
            if clean_dim not in fact_cols:
                errors.append(f"Dimension '{dim}' is missing from fact node '{fact_node.id}'.")

    return PlanCheckResult(
        is_valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
    )
