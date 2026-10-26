import json
import urllib.request
from pathlib import Path

payloads_dir = Path(r"c:\Users\user\Desktop\DS-AgentForge\DS-AgentForge\audit\payloads")

def post_json(url: str, data: dict):
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))

# Load the live generated response from task-374
step1_res = json.loads((payloads_dir / "step1_live_success_response.json").read_text(encoding="utf-8"))
plan_id = step1_res["plan_id"]
generated_plan = step1_res["plan"]

# Write official step1 payloads
step1_req = {
    "requirement": "Calculate Total Net Sales (sum of NETWR from VBAK) and Order Count (sum of Order_Count = 1) grouped by Customer Country (LAND1 from KNA1). Join VBAK with VBRP on VBELN and VBAK with KNA1 on KUNNR. Explicitly qualify join keys as L1_VBAK.VBELN, L1_VBRP.VBELN and L1_VBAK.KUNNR, L1_KNA1.KUNNR.",
    "metadata": json.loads((payloads_dir / "step1_generate_request.json").read_text(encoding="utf-8"))["metadata"],
    "mock_data": json.loads((payloads_dir / "step1_generate_request.json").read_text(encoding="utf-8"))["mock_data"],
    "row_limit": 5,
    "filename": "sap_sd_metadata.json"
}
(payloads_dir / "step1_generate_request.json").write_text(json.dumps(step1_req, indent=2), encoding="utf-8")
(payloads_dir / "step1_generate_response.json").write_text(json.dumps(step1_res, indent=2), encoding="utf-8")

print(f"Step 1 verified: plan_id = {plan_id}")

print("=== STEP 2: Revise Plan ===")
step2_req = {
    "plan_id": plan_id,
    "instruction": "Change join type between VBAK and KNA1 to a left outer join."
}
(payloads_dir / "step2_revise_request.json").write_text(json.dumps(step2_req, indent=2), encoding="utf-8")
step2_res = post_json("http://127.0.0.1:8000/api/plan/revise", step2_req)
(payloads_dir / "step2_revise_response.json").write_text(json.dumps(step2_res, indent=2), encoding="utf-8")

revised_plan = step2_res["plan"]
print(f"Step 2 verified: affected_nodes = {step2_res.get('affected_nodes')}")

print("=== STEP 3: Lineage Synthesize ===")
step3_req = {
    "plan_id": plan_id,
    "plan": revised_plan
}
(payloads_dir / "step3_lineage_synthesize_request.json").write_text(json.dumps(step3_req, indent=2), encoding="utf-8")
step3_res = post_json("http://127.0.0.1:8000/api/lineage/synthesize", step3_req)
(payloads_dir / "step3_lineage_synthesize_response.json").write_text(json.dumps(step3_res, indent=2), encoding="utf-8")

print(f"Step 3 verified: synthesized views = {list(step3_res.keys())}")
print("ALL TASK 2 PAYLOADS RECORDED LIVE!")
