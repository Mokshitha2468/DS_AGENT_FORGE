import json
import sys
from pathlib import Path

# Add backend to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.agent.plan_models import Plan, PlanNode, NodeDetails, PlanMeasure
from app.agent.cds_extractor import parse_csn_metadata
from app.agent.plan_checker import check_plan
from app.agent.view_graph import synthesize_view_graphs_from_plan, NodeType

print("================================================================================")
print("REPRODUCTION RESULTS FOR BACKEND DEFECTS (D2, D3, D4, D6, D7, D8, D11, D12, D13, D14, D15)")
print("================================================================================")

# ── D2 REPRO ──────────────────────────────────────────────────────────────────
print("\n[D2] PlanNode drops is_wrapper / is_projection_enabled / is_filter_enabled:")
node_with_flags = {
    "id": "WRAP_VBAK",
    "operation": "projection",
    "inputs": ["VBAK"],
    "columns": ["VBELN", "KUNNR"],
    "is_wrapper": True,
    "is_projection_enabled": True,
    "is_filter_enabled": True,
    "details": {"condition": "STATUS = 'Completed'"},
    "layer": "L1"
}
validated_node = PlanNode.model_validate(node_with_flags)
dumped = validated_node.model_dump()
print("Input dict keys with wrapper flags:", [k for k in node_with_flags.keys() if "is_" in k])
print("Output model_dump keys:", [k for k in dumped.keys() if "is_" in k])
has_wrapper_flag = hasattr(validated_node, "is_wrapper") or "is_wrapper" in dumped
has_proj_flag = hasattr(validated_node, "is_projection_enabled") or "is_projection_enabled" in dumped
has_filt_flag = hasattr(validated_node, "is_filter_enabled") or "is_filter_enabled" in dumped
d2_failed = not (has_wrapper_flag and has_proj_flag and has_filt_flag)
print("Are is_wrapper/is_projection_enabled/is_filter_enabled preserved?", not d2_failed)
print("--> D2 VERDICT:", "FAIL (Defect PRESENT - wrapper flags silently dropped)" if d2_failed else "PASS")

# ── D6 REPRO ──────────────────────────────────────────────────────────────────
print("\n[D6] view_graph.synthesize_view_graphs_from_plan creates graphs only for L3/L4, none for wrappers:")
sample_plan = Plan(
    analysis=["Test"],
    nodes=[
        PlanNode(id="VBAK", operation="source", inputs=[], columns=["VBELN", "KUNNR"], layer="L0"),
        PlanNode(id="WRAP_VBAK", operation="projection", inputs=["VBAK"], columns=["VBELN", "KUNNR"], layer="L1"),
        PlanNode(id="L3_FACT", operation="join", inputs=["WRAP_VBAK"], columns=["VBELN"], layer="L3"),
        PlanNode(id="L4_MODEL", operation="analytic_model", inputs=["L3_FACT"], columns=["VBELN"], layer="L4"),
    ],
    measures=[PlanMeasure(name="M1", aggregation="SUM", column="VBELN")],
    dimensions=["VBELN"]
)
graphs = synthesize_view_graphs_from_plan(sample_plan)
print("Synthesized view graph IDs:", list(graphs.keys()))
has_wrapper_graph = "WRAP_VBAK" in graphs
d6_failed = not has_wrapper_graph
print("Does WRAP_VBAK have a synthesized view graph?", has_wrapper_graph)
print("--> D6 VERDICT:", "FAIL (Defect PRESENT - wrappers excluded from view graphs)" if d6_failed else "PASS")

# ── D7 REPRO ──────────────────────────────────────────────────────────────────
print("\n[D7] 3+ join inputs: single join node with 3 inputs, no join chaining in view_graph:")
plan_3inputs = Plan(
    analysis=["Test"],
    nodes=[
        PlanNode(id="T1", operation="source", inputs=[], columns=["ID", "A"], layer="L0"),
        PlanNode(id="T2", operation="source", inputs=[], columns=["ID", "B"], layer="L0"),
        PlanNode(id="T3", operation="source", inputs=[], columns=["ID", "C"], layer="L0"),
        PlanNode(
            id="L3_FACT_3WAY",
            operation="join",
            inputs=["T1", "T2", "T3"],
            columns=["ID", "A", "B", "C"],
            details=NodeDetails(
                join_type="inner",
                on=[["T1.ID", "T2.ID"], ["T1.ID", "T3.ID"]]
            ),
            layer="L3"
        ),
    ]
)
vg_3way = synthesize_view_graphs_from_plan(plan_3inputs)["L3_FACT_3WAY"]
join_nodes = [n for n in vg_3way.nodes if n.node_type == NodeType.JOIN]
print("Number of Join nodes in 3-way join graph:", len(join_nodes))
join_incoming_edges = [e for e in vg_3way.edges if e.target == join_nodes[0].id]
print("Number of incoming edges to the single Join node:", len(join_incoming_edges))
d7_failed = len(join_nodes) == 1 and len(join_incoming_edges) == 3
print("--> D7 VERDICT:", "FAIL (Defect PRESENT - non-binary unchained join node)" if d7_failed else "PASS")

# ── D8 REPRO ──────────────────────────────────────────────────────────────────
print("\n[D8] view_graph.py hardcoded sample scenario logic:")
import inspect
from app.agent import view_graph
vg_src = inspect.getsource(view_graph.synthesize_view_graphs_from_plan)
hardcoded_indicators = [
    "ZIPOHDRAPT" in vg_src,
    "V_PO_FINAL_DELIVERY_APT" in vg_src,
    "CALC_PROCESSING_DAYS" in vg_src,
    "PURCHASEORDER" in vg_src,
    "['ID', 'CODE', 'NAME', 'VALUE']" in vg_src or '["ID", "CODE", "NAME", "VALUE"]' in vg_src
]
print("Hardcoded strings found in synthesize_view_graphs_from_plan:", {
    "ZIPOHDRAPT": "ZIPOHDRAPT" in vg_src,
    "V_PO_FINAL_DELIVERY_APT": "V_PO_FINAL_DELIVERY_APT" in vg_src,
    "CALC_PROCESSING_DAYS": "CALC_PROCESSING_DAYS" in vg_src,
    "PURCHASEORDER": "PURCHASEORDER" in vg_src,
    "fallback dummy columns [ID, CODE, NAME, VALUE]": '["ID", "CODE", "NAME", "VALUE"]' in vg_src
})
d8_failed = all(hardcoded_indicators)
print("--> D8 VERDICT:", "FAIL (Defect PRESENT - hardcoded PO scenario assumptions)" if d8_failed else "PASS")

# ── D11 REPRO ─────────────────────────────────────────────────────────────────
print("\n[D11] check_plan rejects ratio KPI formulas in PlanMeasure.column:")
meta_sd = parse_csn_metadata([
    {"name": "SALES", "columns": [{"name": "NETWR", "data_type": "DECIMAL"}, {"name": "ORDERS", "data_type": "INTEGER"}]}
])
plan_ratio = Plan(
    analysis=["Test"],
    nodes=[
        PlanNode(id="SALES", operation="source", inputs=[], columns=["NETWR", "ORDERS"], layer="L0"),
        PlanNode(id="L3_FACT", operation="join", inputs=["SALES"], columns=["NETWR", "ORDERS"], layer="L3"),
    ],
    measures=[
        PlanMeasure(name="AVG_ORDER_VAL", aggregation="DIVIDE", column="NETWR / ORDERS")
    ],
    dimensions=[]
)
check_res_d11 = check_plan(plan_ratio, meta_sd)
print("check_plan errors on ratio measure:", check_res_d11.errors)
d11_failed = any("references column 'NETWR / ORDERS' which does not exist" in err for err in check_res_d11.errors)
print("--> D11 VERDICT:", "FAIL (Defect PRESENT - ratio KPIs rejected by check_plan)" if d11_failed else "PASS")

# ── D12 REPRO ─────────────────────────────────────────────────────────────────
print("\n[D12] check_plan join key validation defaults right input to node.inputs[1] for bare keys:")
meta_3tbl = parse_csn_metadata([
    {"name": "A", "columns": [{"name": "ID", "data_type": "VARCHAR"}, {"name": "K1", "data_type": "VARCHAR"}]},
    {"name": "B", "columns": [{"name": "ID", "data_type": "VARCHAR"}, {"name": "B_COL", "data_type": "VARCHAR"}]},
    {"name": "C", "columns": [{"name": "K1", "data_type": "VARCHAR"}, {"name": "C_COL", "data_type": "VARCHAR"}]},
])
plan_bare_keys = Plan(
    analysis=["Test"],
    nodes=[
        PlanNode(id="A", operation="source", inputs=[], columns=["ID", "K1"], layer="L0"),
        PlanNode(id="B", operation="source", inputs=[], columns=["ID", "B_COL"], layer="L0"),
        PlanNode(id="C", operation="source", inputs=[], columns=["K1", "C_COL"], layer="L0"),
        PlanNode(
            id="FACT",
            operation="join",
            inputs=["A", "B", "C"],
            columns=["ID", "B_COL", "C_COL"],
            details=NodeDetails(
                join_type="inner",
                on=[["ID", "ID"], ["K1", "K1"]]  # second pair K1 is in C (inputs[2]), not B (inputs[1])
            ),
            layer="L3"
        )
    ],
    measures=[],
    dimensions=[]
)
check_res_d12 = check_plan(plan_bare_keys, meta_3tbl)
print("check_plan errors on 3-input join with bare keys:", check_res_d12.errors)
d12_failed = any("right join key 'K1' not found in input 'B'" in err for err in check_res_d12.errors)
print("--> D12 VERDICT:", "FAIL (Defect PRESENT - bare join keys default to inputs[1] only)" if d12_failed else "PASS")

# ── D13 REPRO ─────────────────────────────────────────────────────────────────
print("\n[D13] check_plan does not strip qualifiers from measure columns:")
plan_qualified_measure = Plan(
    analysis=["Test"],
    nodes=[
        PlanNode(id="A", operation="source", inputs=[], columns=["NETWR"], layer="L0"),
        PlanNode(id="FACT", operation="join", inputs=["A"], columns=["NETWR"], layer="L3")
    ],
    measures=[
        PlanMeasure(name="TOTAL_SALES", aggregation="SUM", column="A.NETWR")
    ],
    dimensions=[]
)
check_res_d13 = check_plan(plan_qualified_measure, meta_3tbl)
print("check_plan errors on qualified measure column 'A.NETWR':", check_res_d13.errors)
d13_failed = any("references column 'A.NETWR' which does not exist" in err for err in check_res_d13.errors)
print("--> D13 VERDICT:", "FAIL (Defect PRESENT - qualified measure column rejected)" if d13_failed else "PASS")

print("\nAll backend reproduction tests executed successfully.")
