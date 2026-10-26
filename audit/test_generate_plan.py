import json
from pathlib import Path

from app.agent.cds_extractor import parse_csn_metadata
from app.agent.profiler import profile_data
from app.agent.planner_node import generate_plan, PlannerError
from app.agent.inspection import InspectionTrace, record_metadata_stage, record_compaction_stage, record_sample_mapping_stage
from app.agent.sample_mapper import parse_and_map_sample_data

metadata_raw = [
    {
        "name": "VBAK",
        "business_name": "Sales Document Header",
        "columns": [
            {"name": "VBELN", "data_type": "NVARCHAR(10)", "is_key": True, "semantic_role": "key"},
            {"name": "ERDAT", "data_type": "DATE", "semantic_role": "date"},
            {"name": "KUNNR", "data_type": "NVARCHAR(10)", "semantic_role": "dimension"},
            {"name": "STATUS", "data_type": "NVARCHAR(20)", "semantic_role": "dimension"},
            {"name": "NETWR", "data_type": "DECIMAL(15,2)", "semantic_role": "measure", "aggregation": "SUM"},
            {"name": "WAERK", "data_type": "NVARCHAR(5)", "semantic_role": "dimension"}
        ]
    },
    {
        "name": "VBRP",
        "business_name": "Sales Document Item",
        "columns": [
            {"name": "VBELN", "data_type": "NVARCHAR(10)", "is_key": True, "semantic_role": "key"},
            {"name": "POSNR", "data_type": "NUMC(6)", "is_key": True, "semantic_role": "key"},
            {"name": "MATNR", "data_type": "NVARCHAR(18)", "semantic_role": "dimension"},
            {"name": "FKIMG", "data_type": "DECIMAL(13,3)", "semantic_role": "measure", "aggregation": "SUM"},
            {"name": "NETWR", "data_type": "DECIMAL(15,2)", "semantic_role": "measure", "aggregation": "SUM"}
        ]
    },
    {
        "name": "KNA1",
        "business_name": "Customer Master",
        "columns": [
            {"name": "KUNNR", "data_type": "NVARCHAR(10)", "is_key": True, "semantic_role": "key"},
            {"name": "NAME1", "data_type": "NVARCHAR(35)", "semantic_role": "dimension"},
            {"name": "LAND1", "data_type": "NVARCHAR(3)", "semantic_role": "dimension"},
            {"name": "REGIO", "data_type": "NVARCHAR(3)", "semantic_role": "dimension"},
            {"name": "ORT01", "data_type": "NVARCHAR(35)", "semantic_role": "dimension"}
        ]
    }
]

mock_data = {
    "VBAK": "VBELN,ERDAT,KUNNR,STATUS,NETWR,WAERK\n1001,20260101,CUST1,Completed,500.00,EUR\n1002,20260102,CUST2,Completed,1200.00,EUR\n1003,20260103,CUST1,Open,300.00,EUR\n",
    "VBRP": "VBELN,POSNR,MATNR,FKIMG,NETWR\n1001,000010,MAT_A,5.0,250.00\n1001,000020,MAT_B,5.0,250.00\n1002,000010,MAT_A,10.0,1200.00\n1003,000010,MAT_C,2.0,300.00\n",
    "KNA1": "KUNNR,NAME1,LAND1,REGIO,ORT01\nCUST1,Acme Corp,US,NY,New York\nCUST2,Global Industries,DE,BY,Munich\n"
}

meta = parse_csn_metadata(metadata_raw)
stage_meta = record_metadata_stage(meta)
stage_comp = record_compaction_stage(meta)
sample_res = parse_and_map_sample_data(raw_sample_text=None, metadata=meta)
stage_sample = record_sample_mapping_stage(sample_res)
prof = profile_data(meta, tables_data=mock_data)

trace = InspectionTrace(
    request_id="test-req-1",
    timestamp="2026-10-26T00:00:00Z",
    requirement="Test",
    stage_metadata=stage_meta,
    stage_compaction=stage_comp,
    stage_sample_mapping=stage_sample,
)

req = (
    "Calculate Total Net Sales (sum of NETWR from VBAK) and Average Sales Per Order "
    "(ratio KPI: Total Net Sales / Order Count) grouped by Customer Country (LAND1 from KNA1). "
    "Join Sales Header VBAK with Customer Master KNA1 on KUNNR, and join Item VBRP with Header VBAK on VBELN. "
    "Ensure join keys are explicitly qualified with table names (e.g. VBAK.VBELN, VBRP.VBELN, VBAK.KUNNR, KNA1.KUNNR)."
)

print("Running generate_plan...")
try:
    plan = generate_plan(
        requirement=req,
        metadata=meta,
        profile=prof,
        sample_data_text=sample_res.to_compact_text(),
        max_retries=3,
        inspection_trace=trace,
    )
    print("SUCCESS! Generated plan:")
    print(plan.model_dump_json(indent=2))
except PlannerError as e:
    print("FAILED with PlannerError:", e)
    for i, att in enumerate(trace.stage_llm.attempts):
        print(f"\n--- Attempt {i+1} ---")
        print("Errors:", att.errors)
        print("Raw Response:\n", att.raw_response)
