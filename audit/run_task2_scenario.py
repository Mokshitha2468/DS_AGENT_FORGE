import json
import urllib.request
import urllib.error
from pathlib import Path

payloads_dir = Path(r"c:\Users\user\Desktop\DS-AgentForge\DS-AgentForge\audit\payloads")
payloads_dir.mkdir(parents=True, exist_ok=True)

# 3 tables: VBAK (Sales Document Header), VBRP (Sales Document Item), KNA1 (Customer Master)
metadata = [
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

requirement = (
    "Calculate Total Net Sales (base KPI: sum of NETWR from VBAK) and Average Sales Per Order "
    "(ratio KPI: Total Net Sales / Order Count) grouped by Customer Country (LAND1 from KNA1). "
    "Join Sales Header VBAK with Customer Master KNA1 on KUNNR, and join Item VBRP with Header VBAK on VBELN. "
    "Ensure join keys are explicitly qualified with wrapper aliases: L1_VBAK.VBELN, L1_VBRP.VBELN and L1_VBAK.KUNNR, L1_KNA1.KUNNR. "
    "For the ratio KPI measure, reference the base measure column and define the formula on the analytic model node details."
)

def post_json(url: str, data: dict):
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8")
        print(f"ERROR calling {url} (HTTP {e.code}):\n{err_msg}")
        raise

print("=== STEP 1: Generate Plan ===")
step1_req = {
    "requirement": requirement,
    "metadata": metadata,
    "mock_data": mock_data,
    "row_limit": 5,
    "filename": "sap_sd_metadata.json"
}
(payloads_dir / "step1_generate_request.json").write_text(json.dumps(step1_req, indent=2), encoding="utf-8")

step1_res = post_json("http://127.0.0.1:8000/api/plan", step1_req)
(payloads_dir / "step1_generate_response.json").write_text(json.dumps(step1_res, indent=2), encoding="utf-8")

plan_id = step1_res["plan_id"]
generated_plan = step1_res["plan"]
print(f"Plan generated successfully! Plan ID: {plan_id}")
print(f"Nodes in generated plan: {[n['id'] for n in generated_plan['nodes']]}")
print(f"Measures in generated plan: {generated_plan.get('measures')}")

print("\n=== STEP 2: Revise Plan via Quick AI instruction ===")
step2_req = {
    "plan_id": plan_id,
    "instruction": "Change join type between VBAK and KNA1 to a left outer join."
}
(payloads_dir / "step2_revise_request.json").write_text(json.dumps(step2_req, indent=2), encoding="utf-8")

step2_res = post_json("http://127.0.0.1:8000/api/plan/revise", step2_req)
(payloads_dir / "step2_revise_response.json").write_text(json.dumps(step2_res, indent=2), encoding="utf-8")

revised_plan = step2_res["plan"]
print(f"Plan revised! Affected nodes: {step2_res.get('affected_nodes')}")

print("\n=== STEP 3: Lineage Synthesize ===")
step3_req = {
    "plan_id": plan_id,
    "plan": revised_plan
}
(payloads_dir / "step3_lineage_synthesize_request.json").write_text(json.dumps(step3_req, indent=2), encoding="utf-8")

step3_res = post_json("http://127.0.0.1:8000/api/lineage/synthesize", step3_req)
(payloads_dir / "step3_lineage_synthesize_response.json").write_text(json.dumps(step3_res, indent=2), encoding="utf-8")

print(f"Lineage synthesized successfully! Views synthesized: {list(step3_res.keys())}")
print("\nTask 2 scenario finished and payloads recorded into /audit/payloads/!")
