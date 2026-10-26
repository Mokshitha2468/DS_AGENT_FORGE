import json
import os
import sys
from pathlib import Path

# Add backend to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.agent.cds_extractor import parse_csn_metadata
from app.agent.profiler import profile_data
from app.agent.planner_node import generate_plan, PlannerError
from app.agent.inspection import (
    InspectionTrace,
    record_metadata_stage,
    record_compaction_stage,
    record_sample_mapping_stage,
)
from app.agent.sample_mapper import parse_and_map_sample_data

METADATA_SD = [
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

MOCK_DATA = {
    "VBAK": "VBELN,ERDAT,KUNNR,STATUS,NETWR,WAERK\n1001,20260101,CUST1,Completed,500.00,EUR\n1002,20260102,CUST2,Completed,1200.00,EUR\n1003,20260103,CUST1,Open,300.00,EUR\n",
    "VBRP": "VBELN,POSNR,MATNR,FKIMG,NETWR\n1001,000010,MAT_A,5.0,250.00\n1001,000020,MAT_B,5.0,250.00\n1002,000010,MAT_A,10.0,1200.00\n1003,000010,MAT_C,2.0,300.00\n",
    "KNA1": "KUNNR,NAME1,LAND1,REGIO,ORT01\nCUST1,Acme Corp,US,NY,New York\nCUST2,Global Industries,DE,BY,Munich\n"
}

def execute_case(name: str, requirement: str, run_num: int):
    print(f"\n==================== {name} - RUN {run_num} ====================")
    meta = parse_csn_metadata(METADATA_SD)
    prof = profile_data(meta, tables_data=MOCK_DATA)
    sample_res = parse_and_map_sample_data(raw_sample_text=None, metadata=meta)
    
    trace = InspectionTrace(
        request_id=f"{name}-run{run_num}",
        timestamp="2026-10-26T00:00:00Z",
        requirement=requirement,
        stage_metadata=record_metadata_stage(meta),
        stage_compaction=record_compaction_stage(meta),
        stage_sample_mapping=record_sample_mapping_stage(sample_res),
    )
    
    try:
        plan = generate_plan(
            requirement=requirement,
            metadata=meta,
            profile=prof,
            sample_data_text=sample_res.to_compact_text(),
            max_retries=3,
            inspection_trace=trace,
        )
        print("STATUS: SUCCESS")
        print(f"Attempts taken: {len(trace.stage_llm.attempts)}")
        res_data = {
            "status": "success",
            "attempts": len(trace.stage_llm.attempts),
            "plan": plan.model_dump(),
            "attempt_errors": [att.errors for att in trace.stage_llm.attempts]
        }
        return res_data
    except PlannerError as e:
        print("STATUS: FAILED validation")
        print(f"Attempts taken: {len(trace.stage_llm.attempts)}")
        print("Error details:", str(e))
        last_resp = trace.stage_llm.attempts[-1].raw_response if trace.stage_llm.attempts else ""
        res_data = {
            "status": "failed",
            "attempts": len(trace.stage_llm.attempts),
            "error": str(e),
            "last_response": last_resp,
            "attempt_errors": [att.errors for att in trace.stage_llm.attempts]
        }
        return res_data

cases = [
    (
        "case_a_clean",
        "Calculate Total Net Sales (sum of NETWR from VBAK) and Billed Quantity (sum of FKIMG from VBRP) grouped by Customer Name (NAME1 from KNA1). Join VBAK with VBRP on VBELN and VBAK with KNA1 on KUNNR."
    ),
    (
        "case_b_missing_col",
        "Calculate Net Margin Percentage = (NETWR - DISCOUNT) / NETWR grouped by Customer Country (LAND1). Join VBAK with KNA1 on KUNNR."
    ),
    (
        "case_c_two_plausible",
        "Calculate Total Net Sales by Customer Country (LAND1 from KNA1). Note both VBAK and VBRP contain NETWR column."
    )
]

results = {}
for case_name, req in cases:
    run1 = execute_case(case_name, req, 1)
    run2 = execute_case(case_name, req, 2)
    results[case_name] = {"run1": run1, "run2": run2}

out_file = Path(__file__).resolve().parent / "task5_quality_results.json"
out_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
print(f"\nAll cases completed! Results written to {out_file}")
