"""
Unit tests for Step 1:
- Pydantic Plan Models (Plan, PlanNode, NodeDetails, PlanMeasure)
- CDS Metadata Extractor (parsing CSN, identifying tables vs existing CDS views)
- Deterministic Plan Checker (checking existence, no cycles, valid join keys, no hallucinations)
"""
import json
from pathlib import Path
import pytest

from app.agent.plan_models import Plan, PlanNode, NodeDetails, PlanMeasure, CalculatedFieldSpec
from app.agent.cds_extractor import parse_csn_metadata
from app.agent.plan_checker import check_plan

FIXTURE_PATH = Path(__file__).parent.parent.parent / "fixtures" / "reference" / "F_PO_PROCESSING_TIME_APT (1).json"


def test_plan_models_deserialization():
    """Verify that Plan Pydantic model parses valid plan JSON matching planner_system.md format."""
    plan_dict = {
        "analysis": ["Requirement asks for PO Average Processing Time by Supplier."],
        "nodes": [
            {
                "id": "V_PO_FINAL_DELIVERY_APT",
                "operation": "projection",
                "inputs": ["ZIPOHISTEXT"],
                "columns": ["PURCHASEORDER", "FINAL_DELIVERY_DATE"],
                "details": {"join_type": "", "on": [], "condition": "", "formula": "", "aggregation": ""},
                "reason": "Reuses existing CDS view computing max delivery date.",
                "is_existing": True,
                "layer": "L2",
            },
            {
                "id": "F_PO_PROCESSING_TIME",
                "operation": "join",
                "inputs": ["ZIPOHDRAPT", "V_PO_FINAL_DELIVERY_APT"],
                "columns": ["PURCHASEORDER", "SUPPLIER", "CREATIONDATE", "FINAL_DELIVERY_DATE"],
                "details": {
                    "join_type": "inner",
                    "on": [["PURCHASEORDER", "PURCHASEORDER"]],
                    "condition": "",
                    "formula": "",
                    "aggregation": "",
                },
                "reason": "Join PO headers with final delivery dates on PURCHASEORDER.",
                "is_existing": False,
                "layer": "L3",
            },
        ],
        "measures": [
            {"name": "Total_Delivered_Order", "aggregation": "SUM", "column": "PURCHASEORDER"}
        ],
        "dimensions": ["SUPPLIER"],
        "assumptions": ["PO history dates are formatted as YYYYMMDD."],
        "blockers": [],
    }

    plan = Plan.model_validate(plan_dict)
    assert len(plan.nodes) == 2
    assert plan.nodes[0].is_existing is True
    assert plan.nodes[1].operation == "join"
    assert plan.nodes[1].details.join_type == "inner"
    assert plan.measures[0].name == "Total_Delivered_Order"


def test_cds_metadata_extractor_real_csn():
    """Verify that CDS extractor parses actual CSN fixture and identifies tables vs existing views."""
    assert FIXTURE_PATH.exists(), f"Reference fixture not found: {FIXTURE_PATH}"

    with open(FIXTURE_PATH, encoding="utf-8") as f:
        raw_csn = json.load(f)

    meta = parse_csn_metadata(raw_csn)

    # Must find all 4 objects
    assert "ZIPOHDRAPT" in meta.objects
    assert "ZIPOHISTEXT" in meta.objects
    assert "V_PO_FINAL_DELIVERY_APT" in meta.objects
    assert "F_PO_PROCESSING_TIME_APT" in meta.objects

    # Check classifications
    assert meta.objects["ZIPOHDRAPT"].object_type == "table"
    assert meta.objects["ZIPOHISTEXT"].object_type == "table"
    assert meta.objects["V_PO_FINAL_DELIVERY_APT"].object_type == "view"
    assert meta.objects["F_PO_PROCESSING_TIME_APT"].object_type == "fact"

    # Check columns and keys
    hdr = meta.objects["ZIPOHDRAPT"]
    assert "PURCHASEORDER" in hdr.columns
    assert hdr.columns["PURCHASEORDER"].is_key is True
    assert "SUPPLIER" in hdr.columns

    # Check existing view query extraction
    v_delivery = meta.objects["V_PO_FINAL_DELIVERY_APT"]
    assert "ZIPOHISTEXT" in v_delivery.sources
    assert len(v_delivery.filters) > 0
    assert "FINAL_DELIVERY_DATE" in v_delivery.columns

    compact = meta.to_compact_text()
    assert "[TABLE] ZIPOHDRAPT" in compact
    assert "[VIEW] V_PO_FINAL_DELIVERY_APT" in compact
    assert "[FACT] F_PO_PROCESSING_TIME_APT" in compact
    assert "COLUMNS:" in compact


def test_cds_metadata_extractor_excludes_declared_delta_entities():
    metadata = parse_csn_metadata({
        "definitions": {
            "RAD_S3P.I_SUPPLIER": {
                "kind": "entity",
                "@DataWarehouse.delta": {"deltaFromEntities": ["I_SUPPLIER_Delta"]},
                "elements": {"Supplier": {"type": "cds.String"}},
            },
            "RAD_S3P.I_SUPPLIER_Delta": {
                "kind": "entity",
                "elements": {"Supplier": {"type": "cds.String"}},
            },
        }
    })

    assert "RAD_S3P.I_SUPPLIER" in metadata.objects
    assert "RAD_S3P.I_SUPPLIER_Delta" not in metadata.objects

def test_cds_metadata_extractor_excludes_unannotated_delta_entities():
    metadata = parse_csn_metadata({
        "definitions": {
            "I_SUPPLIER": {
                "kind": "entity",
                "elements": {"Supplier": {"type": "cds.String"}},
            },
            "I_SUPPLIER_Delta": {
                "kind": "entity",
                "elements": {"Supplier": {"type": "cds.String"}},
            },
        }
    })

    assert "I_SUPPLIER" in metadata.objects
    assert "I_SUPPLIER_Delta" not in metadata.objects


def test_plan_checker_valid_plan_with_existing_view():
    """Verify that Plan Checker passes a plan referencing valid tables and existing views."""
    metadata = parse_csn_metadata({
        "definitions": {
            "ZIPOHDRAPT": {
                "kind": "entity",
                "elements": {
                    "PURCHASEORDER": {"type": "cds.String", "key": True},
                    "SUPPLIER": {"type": "cds.String"},
                    "CREATIONDATE": {"type": "cds.String"},
                },
            },
            "V_PO_FINAL_DELIVERY_APT": {
                "kind": "entity",
                "query": {"SELECT": {"from": {"ref": ["ZIPOHISTEXT"]}}},
                "elements": {
                    "PURCHASEORDER": {"type": "cds.String", "key": True},
                    "FINAL_DELIVERY_DATE": {"type": "cds.String"},
                },
            },
        }
    })

    plan = Plan(
        analysis=["Use existing delivery view and join with PO header."],
        nodes=[
            PlanNode(
                id="F_PO_FACT",
                operation="join",
                inputs=["ZIPOHDRAPT", "V_PO_FINAL_DELIVERY_APT"],
                columns=["PURCHASEORDER", "SUPPLIER", "FINAL_DELIVERY_DATE"],
                details=NodeDetails(
                    join_type="inner",
                    on=[["ZIPOHDRAPT.PURCHASEORDER", "V_PO_FINAL_DELIVERY_APT.PURCHASEORDER"]],
                ),
                reason="Join header and delivery date.",
                layer="L3",
            ),
            PlanNode(
                id="AM_PO",
                operation="analytic_model",
                inputs=["F_PO_FACT"],
                columns=["SUPPLIER"],
                reason="Analytic model for POs.",
                layer="L4",
            ),
        ],
        measures=[PlanMeasure(name="Total_Orders", aggregation="COUNT", column="PURCHASEORDER")],
        dimensions=["SUPPLIER"],
    )

    result = check_plan(plan, metadata)
    assert result.is_valid is True
    assert len(result.errors) == 0


def test_plan_checker_rejects_hallucinated_column():
    """Verify that Plan Checker flags columns not present in the input metadata."""
    metadata = parse_csn_metadata({
        "definitions": {
            "ORDERS": {
                "kind": "entity",
                "elements": {
                    "ORDER_ID": {"type": "cds.String", "key": True},
                    "AMOUNT": {"type": "cds.Decimal"},
                },
            }
        }
    })

    plan = Plan(
        nodes=[
            PlanNode(
                id="V_FILTERED",
                operation="projection",
                inputs=["ORDERS"],
                columns=["ORDER_ID", "SECRET_DISCOUNT_CODE"],  # Hallucinated!
                reason="Select discount code.",
            )
        ]
    )

    result = check_plan(plan, metadata)
    assert result.is_valid is False
    assert any("SECRET_DISCOUNT_CODE" in err for err in result.errors)


def test_plan_checker_rejects_nonexistent_input():
    """Verify that Plan Checker flags inputs that do not exist in metadata or earlier steps."""
    metadata = parse_csn_metadata({})

    plan = Plan(
        nodes=[
            PlanNode(
                id="V_FACT",
                operation="projection",
                inputs=["MYTHICAL_TABLE"],  # Does not exist!
                columns=["ID"],
                reason="Test.",
            )
        ]
    )

    result = check_plan(plan, metadata)
    assert result.is_valid is False
    assert any("MYTHICAL_TABLE" in err for err in result.errors)


def test_plan_checker_rejects_cycle():
    """Verify that Plan Checker detects cycles in plan steps."""
    metadata = parse_csn_metadata({
        "definitions": {
            "SRC": {"kind": "entity", "elements": {"ID": {"type": "cds.String"}}}
        }
    })

    plan = Plan(
        nodes=[
            PlanNode(id="STEP_A", operation="projection", inputs=["STEP_B"], columns=["ID"]),
            PlanNode(id="STEP_B", operation="projection", inputs=["STEP_A"], columns=["ID"]),
        ]
    )

    result = check_plan(plan, metadata)
    assert result.is_valid is False
    assert any("Cycle detected" in err for err in result.errors)


def test_plan_checker_rejects_invalid_join_keys():
    """Verify that Plan Checker catches join keys missing from left or right inputs."""
    metadata = parse_csn_metadata({
        "definitions": {
            "T1": {"kind": "entity", "elements": {"ORDER_ID": {"type": "cds.String"}}},
            "T2": {"kind": "entity", "elements": {"CUSTOMER_ID": {"type": "cds.String"}}},
        }
    })

    plan = Plan(
        nodes=[
            PlanNode(
                id="JOIN_VIEW",
                operation="join",
                inputs=["T1", "T2"],
                columns=["ORDER_ID"],
                details=NodeDetails(
                    join_type="inner",
                    on=[["NON_EXISTENT_KEY", "CUSTOMER_ID"]],
                ),
                reason="Join on invalid key.",
            )
        ]
    )

    result = check_plan(plan, metadata)
    assert result.is_valid is False
    assert any("NON_EXISTENT_KEY" in err for err in result.errors)


def test_plan_checker_validates_qualified_keys_in_multi_input_join():
    """Qualified join keys must be checked against their named input, not just inputs 1 and 2."""
    metadata = parse_csn_metadata({
        "definitions": {
            "EKKO": {"kind": "entity", "elements": {
                "EBELN": {"type": "cds.String"},
                "BUKRS": {"type": "cds.String"},
            }},
            "EKPO": {"kind": "entity", "elements": {
                "EBELN": {"type": "cds.String"},
                "EBELP": {"type": "cds.String"},
                "MATNR": {"type": "cds.String"},
            }},
            "EKET": {"kind": "entity", "elements": {
                "EBELN": {"type": "cds.String"},
                "EBELP": {"type": "cds.String"},
                "EINDT": {"type": "cds.String"},
            }},
            "EKBE": {"kind": "entity", "elements": {
                "EBELN": {"type": "cds.String"},
                "EBELP": {"type": "cds.String"},
                "MENGE": {"type": "cds.Decimal"},
            }},
        }
    })
    projections = [
        PlanNode(
            id=f"L1_{table}",
            operation="projection",
            inputs=[table],
            columns=columns,
            reason=f"Project {table} fields.",
            layer="L1",
        )
        for table, columns in (
            ("EKKO", ["EBELN", "BUKRS"]),
            ("EKPO", ["EBELN", "EBELP", "MATNR"]),
            ("EKET", ["EBELN", "EBELP", "EINDT"]),
            ("EKBE", ["EBELN", "EBELP", "MENGE"]),
        )
    ]
    fact = PlanNode(
        id="L3_FACT_SUPPLIER_OTIF",
        operation="join",
        inputs=["L1_EKKO", "L1_EKPO", "L1_EKET", "L1_EKBE"],
        columns=["EBELN", "EBELP", "BUKRS", "MATNR", "EINDT", "MENGE"],
        details=NodeDetails(
            join_type="left",
            on=[
                ["L1_EKKO.EBELN", "L1_EKPO.EBELN"],
                ["L1_EKPO.EBELP", "L1_EKET.EBELP"],
                ["L1_EKPO.EBELP", "L1_EKBE.EBELP"],
            ],
        ),
        reason="Join PO header, items, schedule lines and history.",
        layer="L3",
    )
    model = PlanNode(
        id="L4_MODEL_SUPPLIER_OTIF",
        operation="analytic_model",
        inputs=["L3_FACT_SUPPLIER_OTIF"],
        columns=[],
        reason="Analytic model.",
        layer="L4",
    )

    result = check_plan(Plan(nodes=[*projections, fact, model]), metadata)
    assert result.is_valid is True, result.errors


@pytest.mark.parametrize(
    ("left_key", "expected_error"),
    [
        ("T1.MISSING_KEY", "MISSING_KEY"),
        ("UNKNOWN.ORDER_ID", "not one of the join inputs"),
    ],
)
def test_plan_checker_rejects_invalid_qualified_join_references(left_key, expected_error):
    metadata = parse_csn_metadata({
        "definitions": {
            "T1": {"kind": "entity", "elements": {"ORDER_ID": {"type": "cds.String"}}},
            "T2": {"kind": "entity", "elements": {"CUSTOMER_ID": {"type": "cds.String"}}},
        }
    })
    plan = Plan(nodes=[
        PlanNode(
            id="JOIN_VIEW",
            operation="join",
            inputs=["T1", "T2"],
            details=NodeDetails(
                join_type="inner",
                on=[[left_key, "T2.CUSTOMER_ID"]],
            ),
        )
    ])

    result = check_plan(plan, metadata)
    assert result.is_valid is False
    assert any(expected_error in error for error in result.errors)


def test_plan_checker_still_rejects_case_mismatched_columns():
    """Join resolution must not weaken the exact metadata column-name check."""
    metadata = parse_csn_metadata({
        "definitions": {
            "EKBE": {"kind": "entity", "elements": {"MENGE": {"type": "cds.Decimal"}}},
        }
    })
    plan = Plan(nodes=[
        PlanNode(
            id="L1_EKBE",
            operation="projection",
            inputs=["EKBE"],
            columns=["MENge"],
            reason="Project quantity.",
            layer="L1",
        )
    ])

    result = check_plan(plan, metadata)
    assert result.is_valid is False
    assert any("MENge" in error and "MENGE" in error for error in result.errors)


def test_dynamic_parsing_eket_fixture():
    """Verify that CDS extractor dynamically discovers arbitrary table names (EKET), composite keys, and namespaced columns."""
    eket_fixture_path = Path(__file__).parent / "fixtures" / "EKET.json"
    assert eket_fixture_path.exists(), f"EKET fixture not found: {eket_fixture_path}"

    with open(eket_fixture_path, encoding="utf-8") as f:
        eket_json = json.load(f)

    meta = parse_csn_metadata(eket_json)

    # 1. Dynamic table name discovery (not hardcoded)
    assert "EKET" in meta.objects
    eket = meta.objects["EKET"]

    # 2. Structural classification
    assert eket.object_type == "table"
    assert eket.label == "Scheduling Agreement Schedule Lines"

    # 3. Dynamic composite primary keys
    assert eket.keys == ["MANDT", "EBELN", "EBELP", "ETENR"]
    assert len(eket.keys) == 4

    # 4. Total columns count
    assert len(eket.columns) == 84

    # 5. Namespaced columns and special characters
    assert "/CWM/MENGE" in eket.columns
    assert "/CWM/DABMG" in eket.columns
    assert "_DATAAGING" in eket.columns

    # 6. Measures detection via decimal precision/scale
    menge_col = eket.columns["MENGE"]
    assert menge_col.is_measure is True
    assert "DECIMAL(13,3)" in menge_col.data_type
    assert menge_col.label == "Scheduled Quantity"


def test_plan_checker_rule_1b_strip_qualifiers_and_calculated_field_measures():
    """Rule 1b (D13): Strip table qualifiers from base measures and accept calculated fields."""
    metadata = parse_csn_metadata({
        "definitions": {
            "VBAK": {"kind": "entity", "elements": {
                "VBELN": {"type": "cds.String", "key": True},
                "NETWR": {"type": "cds.Decimal"},
                "TAX": {"type": "cds.Decimal"},
            }}
        }
    })
    plan = Plan(
        nodes=[
            PlanNode(
                id="L1_VBAK",
                operation="projection",
                inputs=["VBAK"],
                columns=["VBELN", "NETWR", "TAX"],
                layer="L1",
            ),
            PlanNode(
                id="L3_FACT",
                operation="join",
                inputs=["L1_VBAK", "L1_VBAK"],
                columns=["VBELN", "NETWR", "TAX", "GROSS_AMOUNT"],
                calculated_fields=[
                    CalculatedFieldSpec(name="GROSS_AMOUNT", formula="NETWR + TAX", label="Gross", data_type="DECIMAL")
                ],
                details=NodeDetails(join_type="inner", on=[["L1_VBAK.VBELN", "L1_VBAK.VBELN"]]),
                layer="L3",
            ),
            PlanNode(
                id="L4_MODEL",
                operation="analytic_model",
                inputs=["L3_FACT"],
                columns=["VBELN"],
                layer="L4",
            )
        ],
        measures=[
            PlanMeasure(name="M_QUALIFIED", aggregation="SUM", column="VBAK.NETWR"),
            PlanMeasure(name="M_CALC_FIELD", aggregation="SUM", column="GROSS_AMOUNT"),
        ],
        dimensions=["VBELN"],
    )
    res = check_plan(plan, metadata)
    assert not any("M_QUALIFIED" in err for err in res.errors)
    assert not any("M_CALC_FIELD" in err for err in res.errors)


def test_plan_checker_rule_1c_formula_measures_and_operator_detection():
    """Rule 1c (D11): Formula measures require valid other measure references; operators in base measures error."""
    metadata = parse_csn_metadata({
        "definitions": {
            "SALES": {"kind": "entity", "elements": {
                "NETWR": {"type": "cds.Decimal"},
                "ORDERS": {"type": "cds.Integer"},
            }}
        }
    })
    # Case 1: Operator in base measure column
    plan_with_op = Plan(
        nodes=[
            PlanNode(id="L1_SALES", operation="projection", inputs=["SALES"], columns=["NETWR", "ORDERS"], layer="L1"),
            PlanNode(id="L3_FACT", operation="join", inputs=["L1_SALES"], columns=["NETWR", "ORDERS"], layer="L3"),
            PlanNode(id="L4_MODEL", operation="analytic_model", inputs=["L3_FACT"], columns=[], layer="L4"),
        ],
        measures=[
            PlanMeasure(name="RATIO_KPI", aggregation="DIVIDE", column="NETWR / ORDERS")
        ],
        dimensions=[],
    )
    res_op = check_plan(plan_with_op, metadata)
    assert any("Move the expression to `formula` and set aggregation to FORMULA." in err for err in res_op.errors)

    # Case 2: Valid FORMULA measure referencing other measures with division guard
    plan_valid_formula = Plan(
        nodes=[
            PlanNode(id="L1_SALES", operation="projection", inputs=["SALES"], columns=["NETWR", "ORDERS"], layer="L1"),
            PlanNode(id="L3_FACT", operation="join", inputs=["L1_SALES"], columns=["NETWR", "ORDERS"], layer="L3"),
            PlanNode(id="L4_MODEL", operation="analytic_model", inputs=["L3_FACT"], columns=[], layer="L4"),
        ],
        measures=[
            PlanMeasure(name="TOTAL_SALES", aggregation="SUM", column="NETWR"),
            PlanMeasure(name="TOTAL_ORDERS", aggregation="SUM", column="ORDERS"),
            PlanMeasure(
                name="AVG_ORDER_VAL",
                aggregation="FORMULA",
                column="",
                formula="CASE WHEN TOTAL_ORDERS = 0 OR TOTAL_ORDERS IS NULL THEN 0 ELSE (TOTAL_SALES / TOTAL_ORDERS) END"
            ),
        ],
        dimensions=[],
    )
    res_formula = check_plan(plan_valid_formula, metadata)
    assert not any("AVG_ORDER_VAL" in err for err in res_formula.errors)

    # Case 3: FORMULA measure referencing undefined measure
    plan_invalid_formula = Plan(
        nodes=[
            PlanNode(id="L1_SALES", operation="projection", inputs=["SALES"], columns=["NETWR"], layer="L1"),
            PlanNode(id="L3_FACT", operation="join", inputs=["L1_SALES"], columns=["NETWR"], layer="L3"),
            PlanNode(id="L4_MODEL", operation="analytic_model", inputs=["L3_FACT"], columns=[], layer="L4"),
        ],
        measures=[
            PlanMeasure(name="TOTAL_SALES", aggregation="SUM", column="NETWR"),
            PlanMeasure(name="AVG_VAL", aggregation="FORMULA", column="", formula="TOTAL_SALES / UNKNOWN_M"),
        ],
        dimensions=[],
    )
    res_inv = check_plan(plan_invalid_formula, metadata)
    assert any("UNKNOWN_M" in err for err in res_inv.errors)


def test_plan_checker_rule_1d_three_input_join_with_bare_keys():
    """Rule 1d (D12): 3-input join resolves bare keys across all inputs and verifies connectivity."""
    metadata = parse_csn_metadata({
        "definitions": {
            "ORDERS": {"kind": "entity", "elements": {
                "ORDER_ID": {"type": "cds.String", "key": True},
                "CUST_FK": {"type": "cds.String"},
                "PROD_FK": {"type": "cds.String"},
            }},
            "CUSTOMERS": {"kind": "entity", "elements": {
                "CUSTOMER_ID": {"type": "cds.String", "key": True},
                "NAME": {"type": "cds.String"},
            }},
            "PRODUCTS": {"kind": "entity", "elements": {
                "PRODUCT_ID": {"type": "cds.String", "key": True},
                "CATEGORY": {"type": "cds.String"},
            }},
        }
    })
    projections = [
        PlanNode(id="L1_ORDERS", operation="projection", inputs=["ORDERS"], columns=["ORDER_ID", "CUST_FK", "PROD_FK"], layer="L1"),
        PlanNode(id="L1_CUSTOMERS", operation="projection", inputs=["CUSTOMERS"], columns=["CUSTOMER_ID", "NAME"], layer="L1"),
        PlanNode(id="L1_PRODUCTS", operation="projection", inputs=["PRODUCTS"], columns=["PRODUCT_ID", "CATEGORY"], layer="L1"),
    ]
    fact = PlanNode(
        id="L3_FACT",
        operation="join",
        inputs=["L1_ORDERS", "L1_CUSTOMERS", "L1_PRODUCTS"],
        columns=["ORDER_ID", "NAME", "CATEGORY"],
        details=NodeDetails(
            join_type="left",
            on=[
                ["CUST_FK", "CUSTOMER_ID"],
                ["PROD_FK", "PRODUCT_ID"],
            ]
        ),
        layer="L3",
    )
    model = PlanNode(id="L4_MODEL", operation="analytic_model", inputs=["L3_FACT"], columns=["NAME", "CATEGORY"], layer="L4")
    plan = Plan(nodes=[*projections, fact, model], measures=[], dimensions=["NAME", "CATEGORY"])

    res = check_plan(plan, metadata)
    assert res.is_valid is True, res.errors


def test_plan_checker_rule_1e_topology_and_dimension_enforcement():
    """Rule 1e: Enforce exactly one fact, exactly one model with fact input, dimensions in fact, no measures in model columns."""
    metadata = parse_csn_metadata({
        "definitions": {
            "T1": {"kind": "entity", "elements": {
                "ID": {"type": "cds.String", "key": True},
                "AMT": {"type": "cds.Decimal"},
            }}
        }
    })
    # Omit L4 model
    plan_no_l4 = Plan(
        nodes=[
            PlanNode(id="L1_T1", operation="projection", inputs=["T1"], columns=["ID", "AMT"], layer="L1"),
            PlanNode(id="L3_FACT", operation="join", inputs=["L1_T1", "L1_T1"], columns=["ID", "AMT"], details=NodeDetails(join_type="inner", on=[["L1_T1.ID", "L1_T1.ID"]]), layer="L3"),
        ],
        measures=[],
        dimensions=["ID"],
    )
    res_no_l4 = check_plan(plan_no_l4, metadata)
    assert any("Plan must contain exactly one analytic_model node" in err for err in res_no_l4.errors)

    # Dimension not in fact output columns
    plan_bad_dim = Plan(
        nodes=[
            PlanNode(id="L1_T1", operation="projection", inputs=["T1"], columns=["ID", "AMT"], layer="L1"),
            PlanNode(id="L3_FACT", operation="join", inputs=["L1_T1", "L1_T1"], columns=["ID", "AMT"], details=NodeDetails(join_type="inner", on=[["L1_T1.ID", "L1_T1.ID"]]), layer="L3"),
            PlanNode(id="L4_MODEL", operation="analytic_model", inputs=["L3_FACT"], columns=["ID"], layer="L4"),
        ],
        measures=[],
        dimensions=["ID", "UNKNOWN_DIM"],
    )
    res_bad_dim = check_plan(plan_bad_dim, metadata)
    assert any("Dimension 'UNKNOWN_DIM' not found in fact node" in err for err in res_bad_dim.errors)

    # Analytic model listing measure in its columns
    plan_measure_in_model = Plan(
        nodes=[
            PlanNode(id="L1_T1", operation="projection", inputs=["T1"], columns=["ID", "AMT"], layer="L1"),
            PlanNode(id="L3_FACT", operation="join", inputs=["L1_T1", "L1_T1"], columns=["ID", "AMT"], details=NodeDetails(join_type="inner", on=[["L1_T1.ID", "L1_T1.ID"]]), layer="L3"),
            PlanNode(id="L4_MODEL", operation="analytic_model", inputs=["L3_FACT"], columns=["ID", "TOTAL_AMT"], layer="L4"),
        ],
        measures=[PlanMeasure(name="TOTAL_AMT", aggregation="SUM", column="AMT")],
        dimensions=["ID"],
    )
    res_measure_in_model = check_plan(plan_measure_in_model, metadata)
    assert any("is a measure name; analytic model columns must contain dimension columns only" in err for err in res_measure_in_model.errors)


def test_wrapper_flags_survive_plan_serialization():
    """Verify that is_wrapper, is_projection_enabled, is_filter_enabled survive model_validate and model_dump."""
    plan_dict = {
        "analysis": ["Test wrapper roundtrip"],
        "nodes": [
            {
                "id": "WRAP_ORDERS",
                "operation": "projection",
                "inputs": ["ORDERS"],
                "columns": ["ORDER_ID", "STATUS"],
                "is_wrapper": True,
                "is_projection_enabled": True,
                "is_filter_enabled": True,
                "details": {"condition": "STATUS = 'OPEN'"},
                "layer": "L1",
            },
            {
                "id": "L3_FACT",
                "operation": "join",
                "inputs": ["WRAP_ORDERS", "WRAP_ORDERS"],
                "columns": ["ORDER_ID"],
                "details": {"join_type": "inner", "on": [["WRAP_ORDERS.ORDER_ID", "WRAP_ORDERS.ORDER_ID"]]},
                "layer": "L3",
            },
            {
                "id": "L4_MODEL",
                "operation": "analytic_model",
                "inputs": ["L3_FACT"],
                "columns": ["ORDER_ID"],
                "layer": "L4",
            },
        ],
        "measures": [],
        "dimensions": ["ORDER_ID"],
    }
    plan = Plan.model_validate(plan_dict)
    dumped = plan.model_dump()
    wrapper_dump = next(n for n in dumped["nodes"] if n["id"] == "WRAP_ORDERS")
    assert wrapper_dump["is_wrapper"] is True
    assert wrapper_dump["is_projection_enabled"] is True
    assert wrapper_dump["is_filter_enabled"] is True
    assert wrapper_dump["details"]["condition"] == "STATUS = 'OPEN'"


