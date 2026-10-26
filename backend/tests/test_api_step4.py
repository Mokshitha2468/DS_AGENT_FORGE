"""
Unit tests for Step 4: Database-Free REST API Endpoints.
Tests:
- POST /api/plan (Plan generation)
- POST /api/plan/revise (Direct step edit and instruction revision)
- GET /api/plan/{id} (Plan retrieval)
- Error handling (400, 404, 422)
"""
import copy
import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.agent.llm_provider import FakeLLMProvider, DEFAULT_FIXTURE_PLAN
from app.api.v1.plan_routes import PLAN_SESSIONS

client = TestClient(app)

FIXTURE_PATH = Path(__file__).parent.parent.parent / "fixtures" / "reference" / "F_PO_PROCESSING_TIME_APT (1).json"


@pytest.fixture(autouse=True)
def clear_sessions(monkeypatch):
    """Ensure in-memory sessions are cleared and tests use FakeLLMProvider."""
    PLAN_SESSIONS.clear()
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    from app.api.v1 import plan_routes
    monkeypatch.setattr(plan_routes, "get_llm_provider", lambda *args, **kwargs: FakeLLMProvider())


@pytest.fixture
def sample_csn_payload():
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


def test_post_plan_success(sample_csn_payload):
    """Verify POST /api/plan returns 201 Created with valid plan and summaries."""
    payload = {
        "requirement": "Calculate Purchase Order Average Processing Time by Supplier.",
        "metadata": sample_csn_payload,
        "mock_data": {
            "ZIPOHDRAPT": "PURCHASEORDER,COMPANYCODE,SUPPLIER\nPO100,1000,SUP1\n",
            "ZIPOHISTEXT": "PURCHASEORDER,GOODSMOVEMENTTYPE,POSTINGDATE\nPO100,101,20260115\n",
        },
    }

    response = client.post("/api/plan", json=payload)
    assert response.status_code == 201, response.text

    data = response.json()
    assert "plan_id" in data
    assert "plan" in data
    assert len(data["plan"]["nodes"]) == 3
    assert data["plan"]["nodes"][0]["is_existing"] is True
    assert "metadata_summary" in data
    assert "profile_summary" in data
    assert "TABLE ZIPOHDRAPT: 1 rows" in data["profile_summary"]


def test_post_plan_empty_requirement(sample_csn_payload):
    """Verify POST /api/plan returns 400 when requirement is empty."""
    payload = {
        "requirement": "   ",
        "metadata": sample_csn_payload,
    }
    response = client.post("/api/plan", json=payload)
    assert response.status_code == 400
    assert "Requirement text cannot be empty" in response.json()["detail"]


def test_post_plan_empty_metadata():
    """Verify POST /api/plan returns 400 when metadata contains no objects."""
    payload = {
        "requirement": "Calculate APT",
        "metadata": {},
    }
    response = client.post("/api/plan", json=payload)
    assert response.status_code == 400
    assert "No valid tables or CDS views found" in response.json()["detail"]


def test_get_plan_success(sample_csn_payload):
    """Verify GET /api/plan/{id} retrieves an existing session."""
    # 1. Create plan
    post_res = client.post(
        "/api/plan",
        json={"requirement": "Calculate APT", "metadata": sample_csn_payload},
    )
    plan_id = post_res.json()["plan_id"]

    # 2. Get plan
    get_res = client.get(f"/api/plan/{plan_id}")
    assert get_res.status_code == 200

    data = get_res.json()
    assert data["plan_id"] == plan_id
    assert data["requirement"] == "Calculate APT"
    assert len(data["plan"]["nodes"]) == 3


def test_get_plan_not_found():
    """Verify GET /api/plan/{id} returns 404 for non-existent session."""
    response = client.get("/api/plan/non-existent-uuid")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_revise_plan_direct_step_edit(sample_csn_payload):
    """Verify POST /api/plan/revise updates a specific node and tracks downstream dependents."""
    # 1. Create initial plan
    post_res = client.post(
        "/api/plan",
        json={"requirement": "Calculate APT", "metadata": sample_csn_payload},
    )
    plan_id = post_res.json()["plan_id"]
    initial_plan = post_res.json()["plan"]

    # 2. Visually edit F_PO_PROCESSING_TIME_APT to be a LEFT join
    node_to_edit = copy.deepcopy(initial_plan["nodes"][1])
    assert node_to_edit["id"] == "F_PO_PROCESSING_TIME_APT"
    node_to_edit["details"]["join_type"] = "left"
    node_to_edit["reason"] = "Changed to LEFT join to preserve all PO headers."

    revise_payload = {
        "plan_id": plan_id,
        "edited_node": node_to_edit,
    }

    revise_res = client.post("/api/plan/revise", json=revise_payload)
    assert revise_res.status_code == 200, revise_res.text

    revised_data = revise_res.json()
    assert revised_data["plan_id"] == plan_id
    # Affected nodes should include the edited node and downstream dependents
    assert "F_PO_PROCESSING_TIME_APT" in revised_data["affected_nodes"]
    assert "AM_PO_APT" in revised_data["affected_nodes"]

    # Verify updated node in returned plan
    updated_node = next(n for n in revised_data["plan"]["nodes"] if n["id"] == "F_PO_PROCESSING_TIME_APT")
    assert updated_node["details"]["join_type"] == "left"


def test_revise_plan_invalid_node_edit(sample_csn_payload):
    """Verify POST /api/plan/revise rejects edits that introduce invalid join keys."""
    post_res = client.post(
        "/api/plan",
        json={"requirement": "Calculate APT", "metadata": sample_csn_payload},
    )
    plan_id = post_res.json()["plan_id"]
    initial_plan = post_res.json()["plan"]

    # Make an invalid join key edit
    node_to_edit = copy.deepcopy(initial_plan["nodes"][1])
    node_to_edit["details"]["on"] = [["ZIPOHDRAPT.PURCHASEORDER", "NON_EXISTENT_KEY"]]

    revise_res = client.post(
        "/api/plan/revise",
        json={"plan_id": plan_id, "edited_node": node_to_edit},
    )
    assert revise_res.status_code == 400
    assert "NON_EXISTENT_KEY" in revise_res.json()["detail"]


def test_revise_plan_not_found():
    """Verify POST /api/plan/revise returns 404 for unknown plan_id."""
    response = client.post(
        "/api/plan/revise",
        json={"plan_id": "ghost-id", "instruction": "Do something"},
    )
    assert response.status_code == 404


def test_revise_plan_instruction_success(sample_csn_payload):
    """Verify POST /api/plan/revise with natural language instruction executes without NameError."""
    post_res = client.post(
        "/api/plan",
        json={"requirement": "Calculate APT", "metadata": sample_csn_payload},
    )
    assert post_res.status_code == 201
    plan_id = post_res.json()["plan_id"]

    revise_res = client.post(
        "/api/plan/revise",
        json={
            "plan_id": plan_id,
            "instruction": "Change join to left outer join and keep other nodes intact",
        },
    )
    assert revise_res.status_code == 200
    data = revise_res.json()
    assert data["plan_id"] == plan_id
    assert "plan" in data
    assert isinstance(data["affected_nodes"], list)


def test_revise_plan_with_client_plan_preserves_wrapper(sample_csn_payload, monkeypatch):
    """Verify that when a client passes current plan state with wrapper filter/projection, it replaces session plan and survives revision."""
    fake_llm = FakeLLMProvider()
    from app.api.v1 import plan_routes
    monkeypatch.setattr(plan_routes, "get_llm_provider", lambda *args, **kwargs: fake_llm)

    post_res = client.post(
        "/api/plan",
        json={"requirement": "Calculate APT", "metadata": sample_csn_payload},
    )
    assert post_res.status_code == 201
    plan_id = post_res.json()["plan_id"]
    current_plan = post_res.json()["plan"]

    # Client configures wrapper projection and filter
    wrapper_node = {
        "id": "WRAP_ZIPOHDRAPT",
        "operation": "projection",
        "inputs": ["ZIPOHDRAPT"],
        "columns": ["PURCHASEORDER", "COMPANYCODE", "SUPPLIER"],
        "details": {"condition": "COMPANYCODE = '1000'"},
        "reason": "Wrapper for ZIPOHDRAPT",
        "is_wrapper": True,
        "is_projection_enabled": True,
        "is_filter_enabled": True,
        "layer": "L1",
    }
    client_plan = copy.deepcopy(current_plan)
    client_plan["nodes"].insert(0, wrapper_node)

    # Queue revised response retaining the wrapper
    revised_plan_payload = copy.deepcopy(client_plan)
    fake_llm.queue_response(json.dumps(revised_plan_payload))

    revise_res = client.post(
        "/api/plan/revise",
        json={
            "plan_id": plan_id,
            "instruction": "Keep wrapper and adjust fact",
            "plan": client_plan,
        },
    )
    assert revise_res.status_code == 200, revise_res.text
    revised_data = revise_res.json()
    wrapper_out = next(n for n in revised_data["plan"]["nodes"] if n["id"] == "WRAP_ZIPOHDRAPT")
    assert wrapper_out["is_wrapper"] is True
    assert wrapper_out["is_projection_enabled"] is True
    assert wrapper_out["is_filter_enabled"] is True
    assert wrapper_out["details"]["condition"] == "COMPANYCODE = '1000'"

    # Verify that the LLM received the client's wrapper in its revision prompt
    assert len(fake_llm.call_history) == 1
    assert "COMPANYCODE = '1000'" in fake_llm.call_history[0]["user"]
    assert "WRAP_ZIPOHDRAPT" in fake_llm.call_history[0]["user"]


def test_confirm_plan_success(sample_csn_payload):
    """Verify POST /api/plan/{plan_id}/confirm validates and confirms compliant plan."""
    post_res = client.post(
        "/api/plan",
        json={"requirement": "Calculate APT", "metadata": sample_csn_payload},
    )
    assert post_res.status_code == 201
    plan_id = post_res.json()["plan_id"]

    compliant_plan = {
        "analysis": ["Compliant PO plan"],
        "nodes": [
            {
                "id": "ZIPOHDRAPT",
                "operation": "source",
                "columns": ["PURCHASEORDER", "COMPANYCODE", "SUPPLIER"],
                "is_existing": True,
                "layer": "L0",
            },
            {
                "id": "V_PO_FINAL_DELIVERY_APT",
                "operation": "source",
                "columns": ["PURCHASEORDER", "FINAL_DELIVERY_DATE"],
                "is_existing": True,
                "layer": "L0",
            },
            {
                "id": "MN_WRAP_ZIPOHDRAPT",
                "operation": "projection",
                "inputs": ["ZIPOHDRAPT"],
                "columns": ["PURCHASEORDER", "COMPANYCODE", "SUPPLIER"],
                "is_wrapper": True,
                "is_projection_enabled": True,
                "is_filter_enabled": True,
                "details": {"condition": "COMPANYCODE = '1000'"},
                "layer": "L1",
            },
            {
                "id": "MN_WRAP_V_PO_FINAL_DELIVERY_APT",
                "operation": "projection",
                "inputs": ["V_PO_FINAL_DELIVERY_APT"],
                "columns": ["PURCHASEORDER", "FINAL_DELIVERY_DATE"],
                "is_wrapper": True,
                "is_projection_enabled": False,
                "is_filter_enabled": False,
                "details": {},
                "layer": "L1",
            },
            {
                "id": "F_PO_PROCESSING_TIME_APT",
                "operation": "join",
                "inputs": ["MN_WRAP_ZIPOHDRAPT", "MN_WRAP_V_PO_FINAL_DELIVERY_APT"],
                "columns": ["PURCHASEORDER", "COMPANYCODE", "FINAL_DELIVERY_DATE"],
                "details": {
                    "join_type": "inner",
                    "on": [["MN_WRAP_ZIPOHDRAPT.PURCHASEORDER", "MN_WRAP_V_PO_FINAL_DELIVERY_APT.PURCHASEORDER"]],
                },
                "layer": "L3",
            },
            {
                "id": "AM_PO_APT",
                "operation": "analytic_model",
                "inputs": ["F_PO_PROCESSING_TIME_APT"],
                "columns": ["PURCHASEORDER", "COMPANYCODE"],
                "details": {},
                "layer": "L4",
            },
        ],
        "measures": [],
        "dimensions": ["PURCHASEORDER", "COMPANYCODE"],
    }

    confirm_res = client.post(
        f"/api/plan/{plan_id}/confirm",
        json={"plan": compliant_plan},
    )
    assert confirm_res.status_code == 200, confirm_res.text
    confirm_data = confirm_res.json()
    assert confirm_data["ok"] is True, confirm_data["errors"]
    assert confirm_data["errors"] == []
    assert confirm_data["confirmed_at"] is not None


def test_confirm_plan_validation_failure_missing_column(sample_csn_payload):
    """Verify POST /api/plan/{plan_id}/confirm rejects plan with missing column in wrapper filter."""
    post_res = client.post(
        "/api/plan",
        json={"requirement": "Calculate APT", "metadata": sample_csn_payload},
    )
    assert post_res.status_code == 201
    plan_id = post_res.json()["plan_id"]

    invalid_plan = {
        "analysis": ["Invalid PO plan"],
        "nodes": [
            {
                "id": "ZIPOHDRAPT",
                "operation": "source",
                "columns": ["PURCHASEORDER", "COMPANYCODE", "SUPPLIER"],
                "is_existing": True,
                "layer": "L0",
            },
            {
                "id": "MN_WRAP_ZIPOHDRAPT",
                "operation": "projection",
                "inputs": ["ZIPOHDRAPT"],
                "columns": ["PURCHASEORDER", "COMPANYCODE", "SUPPLIER"],
                "is_wrapper": True,
                "is_projection_enabled": True,
                "is_filter_enabled": True,
                "details": {"condition": "GHOST_COLUMN = '1000'"},
                "layer": "L1",
            },
            {
                "id": "F_PO_PROCESSING_TIME_APT",
                "operation": "join",
                "inputs": ["MN_WRAP_ZIPOHDRAPT"],
                "columns": ["PURCHASEORDER", "COMPANYCODE"],
                "details": {"join_type": "inner", "on": []},
                "layer": "L3",
            },
            {
                "id": "AM_PO_APT",
                "operation": "analytic_model",
                "inputs": ["F_PO_PROCESSING_TIME_APT"],
                "columns": ["PURCHASEORDER"],
                "layer": "L4",
            },
        ],
        "measures": [],
        "dimensions": ["PURCHASEORDER"],
    }

    confirm_res = client.post(
        f"/api/plan/{plan_id}/confirm",
        json={"plan": invalid_plan},
    )
    assert confirm_res.status_code == 200
    confirm_data = confirm_res.json()
    assert confirm_data["ok"] is False
    assert any(
        "MN_WRAP_ZIPOHDRAPT" in err and "GHOST_COLUMN" in err
        for err in confirm_data["errors"]
    )


def test_confirm_plan_not_found():
    """Verify POST /api/plan/{plan_id}/confirm returns 404 for unknown session."""
    res = client.post("/api/plan/nonexistent-plan/confirm", json={"plan": {"nodes": []}})
    assert res.status_code == 404


