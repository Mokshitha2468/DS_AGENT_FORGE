# DS-AgentForge Audit Report: Chat-to-Lineage Flow Correctness Audit

**Date of Audit:** October 26, 2026  
**Auditor:** Antigravity (Advanced Agentic Pair Programmer)  
**Target Repository:** DS-AgentForge (`/backend` FastAPI, `/frontend` React + TypeScript + Vitest)  
**Execution Environment:** Windows, Python 3.12.6, Node.js v24.14.1, Gemini LLM (`gemini-3.5-flash-lite` / `gemini-2.5-flash`)  

---

## Executive Summary

This read-only audit evaluated the current end-to-end user flow of DS-AgentForge:
**Chat Page (Requirement + CSN/JSON Source Metadata) → Plan Generation (LLM) → Plan Page (Interactive Visualizer, Wrapper Configuration, Quick AI Revision, Accept) → Lineage Page (View-level Graphical Synthesis).**

### Key Findings at a Glance
1. **Pervasive Dead Code:** 43 out of 58 Python modules in `backend/app` are unreachable from `main.py`. Major architectural systems from prior iterations (`agent/nodes.py`, `agent/graph.py`, `services/ds_native_renderer.py`, `api/v1/runs.py`, `api/v1/canvas.py`, `api/v1/chat.py`, `api/v1/views.py`, `api/v1/export.py`) are dead code.
2. **Contract Mismatches Between LLM & Deterministic Checker:** The planner prompt (`planner_system.md`) directs the LLM to output ratio KPIs, but `plan_checker.py` rejects any measure whose `column` contains an arithmetic expression (e.g. `'TotalNetSales / OrderCount'`), because `PlanMeasure` has no `formula` attribute. Furthermore, `check_plan` hardcodes right join key resolution to `node.inputs[1]`, causing deterministic validation failures whenever 3 or more tables are joined with bare column names.
3. **Frontend / Backend State Desynchronization:** Quick AI Revise (`POST /api/plan/revise`) accepts only `{ plan_id, instruction }`. Any manual wrapper modifications, projection column toggles, or filter expressions made on the frontend are never sent, so the backend revises a stale in-memory plan and overwrites manual client edits upon return.
4. **Validation Void at Hand-Off:** Accepting a plan (`handleAcceptPlan`) runs zero validation and calls no backend confirm endpoint; broken or inconsistent plans are passed directly via `sessionStorage` into the Lineage synthesis route (`POST /api/lineage/synthesize`), which generates view graphs without schema checking.
5. **Defect Status Summary:** Of the 10 suspected defects D1–D10, **9 are CONFIRMED PRESENT (FAIL)** and **1 is REFUTED (PASS)**:
   - **FAIL (Defect Present):** D1, D2, D3, D4, D5, D6, D7, D8, D10.
   - **PASS (Defect Not Present):** D9 (`preparePlanPipeline` was proven strictly idempotent).
   - **Additional Defects Discovered (D11–D15):** D11 (`check_plan` measure formula rejection), D12 (3+ input join key positional failure), D13 (measure qualifier stripping failure), D14 (43 unmounted backend modules with frontend 404s), D15 (chat page swallows API failure and navigates anyway).

---

## 1. Flow Map (Live Trace)

The table below traces every user action across the live chat-to-lineage flow.

| Step | User Action | Frontend Component & Function | Client API Call | Backend Route & Controller | Backend Internal Functions Called | LLM Invocation Details | State Storage Location |
|---|---|---|---|---|---|---|---|
| **1** | User enters prompt, pastes/uploads CSN JSON & sample data, clicks "Generate Plan" | [ChatRequirementPage.tsx:243](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/frontend/src/pages/ChatRequirementPage.tsx#L243) `handleSubmit` | `POST /api/plan` ([planApi.ts:8](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/frontend/src/features/plan/planApi.ts#L8) `generatePlanApi`) | `POST /api/plan` ([plan_routes.py:108](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/api/v1/plan_routes.py#L108) `create_plan`) | `parse_csn_metadata`, `record_metadata_stage`, `parse_and_map_sample_data`, `profile_data`, `generate_plan`, `check_plan`, `save_inspection_trace` | **LIVE**: `GeminiLLMProvider.generate()`, model `gemini-3.5-flash-lite`, prompt file `planner_system.md`. Inputs: system prompt + requirement + metadata + data profile + sample rows. Output shape: JSON `Plan` schema. | `sessionStorage` (`activePlan`, `activePlanId`, `activePlanRequestId`, `activeSourceEntities`); Backend: `PLAN_SESSIONS[plan_id]`, `INSPECTION_TRACES` |
| **2** | Visual Plan page loads | [PlanPage.tsx:246](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/frontend/src/features/plan/PlanPage.tsx#L246) `PlanPage` initial mount | None (reads `sessionStorage`) | None | Frontend `preparePlanPipeline` folds projection/filter nodes and adds `WRAP_` wrappers | None | Component local state `plan`, `sessionStorage` |
| **3** | User opens wrapper step editor, toggles projection/filter columns | [PlanPage.tsx:316](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/frontend/src/features/plan/PlanPage.tsx#L316) `handleAddTransform`, `handleManualStepUpdated` | None (purely client-side) | None | Pure client-side state manipulation via `addPlanTransform` | None | Component state `plan`, updated in `sessionStorage.setItem('activePlan')` (NOT synced to backend) |
| **4** | User types natural language instruction in Quick AI Revise bar, clicks "Revise" | [PlanPage.tsx:363](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/frontend/src/features/plan/PlanPage.tsx#L363) `handleQuickRevise` | `POST /api/plan/revise` ([planApi.ts:39](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/frontend/src/features/plan/planApi.ts#L39) `revisePlanApi`) | `POST /api/plan/revise` ([plan_routes.py:254](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/api/v1/plan_routes.py#L254) `revise_plan`) | `_revise_via_llm`, `check_plan` | **LIVE**: `GeminiLLMProvider.generate()`. Inputs: system prompt + `session["plan"]` (stale backend copy) + metadata + instruction. Output shape: JSON `Plan`. | Backend updates `PLAN_SESSIONS[plan_id]["plan"]`; Frontend merges retained wrappers and updates `sessionStorage` |
| **5** | User clicks "Accept Plan" button | [PlanPage.tsx:385](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/frontend/src/features/plan/PlanPage.tsx#L385) `handleAcceptPlan` | None (no endpoint called) | None | None (no confirmation or validation logic) | None | `sessionStorage.setItem('activePlan', ...)`, `navigate('/lineage?view=overview')` |
| **6** | Lineage Page loads and synthesizes graphical views | [LineagePage.tsx:38](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/frontend/src/pages/LineagePage.tsx#L38) `useEffect` | `POST /api/lineage/synthesize` ([lineageApi.ts:8](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/frontend/src/features/lineage/lineageApi.ts#L8) `synthesizeLineageApi`) | `POST /api/lineage/synthesize` ([lineage_routes.py:73](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/api/v1/lineage_routes.py#L73) `synthesize_lineage`) | `synthesize_view_graphs_from_plan`, `build_view_graph_for_proposed_node`, `validate_view_graph` | None | Backend `_VIEW_GRAPHS[plan_id]`, `_VIEW_HISTORY`, `_VIEW_REDO`; Frontend `viewGraphs` state |
| **7** | User adjusts graphical view via Lineage AI revise | [DatasphereGraphicalViewEditor.tsx](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/frontend/src/features/lineage/DatasphereGraphicalViewEditor.tsx) | `POST /api/lineage/{plan_id}/{view_id}/ai` | `POST /api/lineage/{plan_id}/{view_id}/ai` ([lineage_routes.py:257](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/api/v1/lineage_routes.py#L257) `ai_revise_view`) | `revise_view_graph_ai`, `apply_operations`, `validate_view_graph` | **LIVE**: `GeminiLLMProvider.generate()`. Inputs: system prompt + ViewGraph JSON + user instruction. Output shape: JSON `AIRevisionResponse` (`operations` list). | Backend `_VIEW_GRAPHS`, `_VIEW_HISTORY` |

---

## 2. LLM Call Sites Table

All LLM call sites identified across the entire repository:

| Call Site (File & Line) | Function | Provider & Mechanism | Prompt File / Template | Expected Output Shape | Live / Dead Status | Evidence / Reason |
|---|---|---|---|---|---|---|
| [backend/app/agent/planner_node.py:129](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/agent/planner_node.py#L129) | `generate_plan` | `GeminiLLMProvider.generate` / `LiteLLMProvider.generate` via `get_llm_provider()` | `backend/app/agent/prompts/planner_system.md` | JSON conforming to Pydantic `Plan` schema | **LIVE** | Called by `create_plan` in `plan_routes.py:174` (mounted at `POST /api/plan`). Verified live in Task 2. |
| [backend/app/api/v1/plan_routes.py:399](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/api/v1/plan_routes.py#L399) | `_revise_via_llm` | `GeminiLLMProvider.generate` / `LiteLLMProvider.generate` | `backend/app/agent/prompts/planner_system.md` | JSON conforming to Pydantic `Plan` schema | **LIVE** | Called by `revise_plan` in `plan_routes.py:306` (mounted at `POST /api/plan/revise`). Verified live in Task 2. |
| [backend/app/agent/view_graph_ai.py:220](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/agent/view_graph_ai.py#L220) | `revise_view_graph_ai` | `GeminiLLMProvider.generate` / `LiteLLMProvider.generate` | Inline `SYSTEM_PROMPT` in `view_graph_ai.py:27-109` | JSON conforming to `AIRevisionResponse` | **LIVE** | Called by `ai_revise_view` in `lineage_routes.py:257` (mounted at `POST /api/lineage/{plan_id}/{view_id}/ai`). |
| [backend/app/api/v1/chat.py:104](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/api/v1/chat.py#L104) | `chat_endpoint` | `call_llm("modify_view_spec", ...)` via `litellm` | Inline prompt formatting | JSON dict with `action`, `modifications`, `explanation` | **DEAD** | `app.api.v1.chat` is NOT imported or mounted in `main.py`. Verified by AST reachability trace. |
| [backend/app/agent/nodes.py:57](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/agent/nodes.py#L57) | `clarify_requirement_node` | `call_llm("clarify_requirement", ...)` via `litellm` | Inline prompt formatting | JSON dict with clarification questions | **DEAD** | `app.agent.nodes` is NOT imported or mounted anywhere in live runtime. |
| [backend/app/agent/nodes.py:226](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/agent/nodes.py#L226) | `structure_propose_node` | `call_llm("structure_propose", ...)` via `litellm` | Inline prompt formatting | JSON dict with proposed view architecture | **DEAD** | `app.agent.nodes` is NOT imported or mounted anywhere in live runtime. |
| [backend/app/agent/nodes.py:304](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/agent/nodes.py#L304) | `build_view_spec_node` | `call_llm("build_view_spec", ...)` via `litellm` | Inline prompt formatting | JSON dict with full Datasphere ViewSpec | **DEAD** | `app.agent.nodes` is NOT imported or mounted anywhere in live runtime. |
| [backend/app/agent/nodes.py:435](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/agent/nodes.py#L435) | `modify_view_node` | `call_llm("build_view_spec", ...)` via `litellm` | Inline prompt formatting | JSON dict with modified ViewSpec | **DEAD** | `app.agent.nodes` is NOT imported or mounted anywhere in live runtime. |
| [backend/app/agent/llm.py:207](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/agent/llm.py#L207) | `call_llm` | `litellm.completion` | Various | JSON dict | **DEAD** | Only imported by `nodes.py` and `chat.py`, both of which are dead code. |

---

## 3. Dead Code List

### Verification Method
Executed AST recursive import traversal from entrypoint [backend/app/main.py](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/main.py) across all 58 Python modules in `backend/app`. Command: `python audit/trace_reachability.py`.

### Results
- **Total modules in `backend/app`:** 58
- **Reachable modules:** 15
- **Unreachable (Dead) modules:** 43 (74.1% of backend codebase)

### The 15 Reachable Modules (Live Code)
1. `app.main`
2. `app.core.config`
3. `app.api.v1.websocket`
4. `app.api.v1.plan_routes`
5. `app.api.v1.lineage_routes`
6. `app.agent.cds_extractor`
7. `app.agent.inspection`
8. `app.agent.llm_provider`
9. `app.agent.plan_checker`
10. `app.agent.plan_models`
11. `app.agent.planner_node`
12. `app.agent.profiler`
13. `app.agent.sample_mapper`
14. `app.agent.view_graph`
15. `app.agent.view_graph_ai`

### Confirmation of User Hypotheses Regarding Dead Code
| Targeted Module | Claimed Status | Verified Status | Evidence |
|---|---|---|---|
| `backend/app/agent/nodes.py` | Unreachable | **CONFIRMED UNREACHABLE** | Not imported by any of the 15 live modules; 0 references in active route tree. |
| `backend/app/agent/graph.py` | Unreachable | **CONFIRMED UNREACHABLE** | Not imported by `main.py` or active routers. |
| `backend/app/services/ds_native_renderer.py` | Unreachable | **CONFIRMED UNREACHABLE** | Not imported by `main.py` or active routers. |
| `backend/app/agent/datasphere_canvas_builder.py` | Unreachable | **CONFIRMED UNREACHABLE** | Located in `backend/app/agent/datasphere_canvas_builder.py`; not imported by live routers. |
| `backend/app/api/v1/runs.py` | Unreachable | **CONFIRMED UNREACHABLE** | Router never mounted in `main.py`. |
| `backend/app/api/v1/canvas.py` | Unreachable | **CONFIRMED UNREACHABLE** | Router never mounted in `main.py`. |
| `backend/app/api/v1/export.py` | Unreachable | **CONFIRMED UNREACHABLE** | Router never mounted in `main.py`. |
| `backend/app/api/v1/views.py` | Unreachable | **CONFIRMED UNREACHABLE** | Router never mounted in `main.py`. |

### Complete List of 43 Dead Modules
`app.__init__`, `app.agent.datasphere_canvas_builder`, `app.agent.graph`, `app.agent.graph_generator`, `app.agent.json_compressor`, `app.agent.llm`, `app.agent.metadata_parser`, `app.agent.nodes`, `app.agent.sap_dictionary`, `app.agent.states`, `app.api.__init__`, `app.api.v1.__init__`, `app.api.v1.auth_routes`, `app.api.v1.canvas`, `app.api.v1.catalog`, `app.api.v1.chat`, `app.api.v1.connections`, `app.api.v1.export`, `app.api.v1.runs`, `app.api.v1.views`, `app.core.database`, `app.core.security`, `app.ds.auth`, `app.ds.export_import_adapter`, `app.ds.profiler`, `app.ds.reader`, `app.models.__init__`, `app.models.catalog`, `app.models.connection`, `app.models.run`, `app.models.user`, `app.services.association_detector`, `app.services.audit`, `app.services.catalog_service`, `app.services.ds_native_renderer`, `app.services.embedding_service`, `app.services.export_service`, `app.services.renderer_service`, `app.services.replication_builder`, `app.services.sql_generator`, `app.services.validator`, `app.services.view_builder`, `app.worker`.

---

## 4. Plan Correctness Against Its Own Contract (Task 3)

Evaluated against [backend/app/agent/prompts/planner_system.md](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/agent/prompts/planner_system.md), [backend/app/agent/plan_models.py](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/agent/plan_models.py), and [backend/app/agent/plan_checker.py](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/agent/plan_checker.py), referencing the live generated plan payload [audit/payloads/step1_generate_response.json](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/audit/payloads/step1_generate_response.json) and quality run plans.

| Rule / Contract Clause | Pass / Fail | Evidence & Explanation | Label |
|---|---|---|---|
| **Layer structure:** L0 sources, exactly one L1 wrapper per source, exactly one L3 fact view, exactly one L4 analytic model | **FAIL** | In live Step 1 payload (`step1_generate_response.json`), the LLM generated `['L1_VBAK', 'L1_VBRP', 'L1_KNA1', 'L3_FACT_SALES']`. **L4 analytic model node was completely missing from `nodes`**, leaving the plan truncated at the fact layer. | VERIFIED BY RUNNING |
| **Wrappers not created by the LLM:** "wrappers not created by the LLM" vs prompt rule "L1 exactly one configurable wrapper Relational Dataset per source" | **FAIL** | In direct contradiction to the architectural intent of having the frontend generate wrappers, `planner_system.md:6` instructs the LLM: `L1 exactly one configurable wrapper Relational Dataset per source`. The LLM consequently creates nodes `L1_VBAK`, `L1_VBRP`, etc., which collides with `planPipeline.ts` generating `WRAP_VBAK`. | VERIFIED BY RUNNING |
| **Formulas only in calculated_fields:** Fact-level formulas must be distinct entries in `calculated_fields`, not node details formula | **PASS** | `L3_FACT_SALES.calculated_fields` had `[{'name': 'Order_Count', 'formula': '1', 'label': 'Order Count Flag', 'data_type': 'INTEGER'}]`, and `details.formula` was empty. | VERIFIED BY RUNNING |
| **Measure shapes:** Aggregate measures reference valid output columns; ratio KPIs defined separately | **FAIL** | In natural prompt runs (Task 2 & Task 5), whenever a ratio KPI was requested, the LLM placed `'Total_Net_Sales / Order_Count'` into `PlanMeasure.column`. `check_plan.py:84-86` rejected this because no column named `'Total_Net_Sales / Order_Count'` exists, raising HTTP 422. | VERIFIED BY RUNNING |
| **No forbidden nodes:** No L2 helper/staging/dimension views, no delta entities | **PASS** | Generated nodes contained no `L2` nodes or `_DELTA` entities. | VERIFIED BY RUNNING |
| **Every column exists in its input:** Output columns must trace to inputs or calculated fields | **PASS** | `L3_FACT_SALES.columns` (`['VBELN', 'KUNNR', 'NETWR', 'LAND1', 'Order_Count']`) all existed in upstream wrapper projections or `calculated_fields`. | VERIFIED BY RUNNING |
| **Join keys come from details.on and exist on both sides** | **FAIL (Conditional)** | Passed when prompt specifically instructed LLM to use qualified wrapper aliases (`[['L1_VBAK.VBELN', 'L1_VBRP.VBELN']]`). Failed on standard prompts (Case A & Case C Run 1) where LLM produced bare keys (`[['VBELN', 'VBELN'], ['KUNNR', 'KUNNR']]`), triggering `check_plan.py` error `Join node right join key 'KUNNR' not found in input 'L1_VBRP'`. | VERIFIED BY RUNNING |
| **Analytic model columns are dimensions only** | **FAIL** | When the LLM did generate an analytic model (Case C Run 2: `L4_ANALYTIC_SALES`), its `columns` array contained `['L1_LAND1', 'TotalNetSales', 'AverageSalesPerOrder']`, improperly including aggregate measures alongside dimensions. | VERIFIED BY RUNNING |

---

## 5. Defect Table (D1 – D15)

Status definitions: **FAIL** = Defect Present in repository; **PASS** = Defect Not Present (claim refuted).

| Defect ID | Status | Severity | Evidence | Repro Path | File Needing Fix |
|---|---|---|---|---|---|
| **D1** | **FAIL** (Present) | Data-Wrong | In [planPipeline.ts:272-300](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/frontend/src/features/plan/planPipeline.ts#L272-L300), `getRequiredNodeColumns` checks only `downstream` nodes' `columns` and `details.condition/formula`. It completely ignores: 1) the wrapper's own `node.details.condition` (e.g. `STATUS = 'Completed'`), 2) downstream `calculated_fields[].formula`, 3) `plan.measures[].column`, 4) `plan.dimensions`. Vitest test fails at `planPipeline.test.ts:136`. | Run `npx --package=tsx tsx ../audit/repros/run_frontend_repros.ts` | `frontend/src/features/plan/planPipeline.ts` |
| **D2** | **FAIL** (Present) | Data-Wrong | In [plan_models.py:71-87](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/agent/plan_models.py#L71-L87), `PlanNode` defines fields `id, operation, inputs, columns, calculated_fields, details, reason, is_existing, layer`. Because `model_config = ConfigDict(extra="ignore")`, properties `is_wrapper`, `is_projection_enabled`, and `is_filter_enabled` sent by frontend are silently dropped upon round trip through `/api/plan/revise` or `/api/lineage/synthesize`. | Run `$env:PYTHONPATH='backend'; python audit/repros/run_backend_repros.py` | `backend/app/agent/plan_models.py` |
| **D3** | **FAIL** (Present) | Silent | In [PlanPage.tsx:369](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/frontend/src/features/plan/PlanPage.tsx#L369), `handleQuickRevise` calls `revisePlanApi(planId, { instruction: quickInstruction })`. It sends only `plan_id` and `instruction`. In [plan_routes.py:272](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/api/v1/plan_routes.py#L272), backend reads `current_plan = session["plan"]`. Client manual wrapper modifications stored in `sessionStorage` are never sent, so the LLM revises the stale original plan and overwrites manual edits. | Code inspection & verified by running `run_task2_full.py` | `frontend/src/features/plan/PlanPage.tsx` & `backend/app/api/v1/plan_routes.py` |
| **D4** | **FAIL** (Present) | Data-Wrong | In [PlanPage.tsx:385-389](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/frontend/src/features/plan/PlanPage.tsx#L385-L389), `handleAcceptPlan` runs zero validation: `sessionStorage.setItem('activePlan', JSON.stringify(plan)); navigate('/lineage?view=overview')`. In [lineage_routes.py:78-113](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/api/v1/lineage_routes.py#L78-L113), `/api/lineage/synthesize` accepts `plan` with no schema checker, passing invalid plans straight to canvas rendering. | Code inspection | `frontend/src/features/plan/PlanPage.tsx` & `backend/app/api/v1/lineage_routes.py` |
| **D5** | **FAIL** (Present) | Cosmetic / Silent | [planPipeline.ts:58](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/frontend/src/features/plan/planPipeline.ts#L58) creates wrappers with prefix `WRAP_`: `safeNodeId('WRAP', sourceName)`. But [planExport.test.ts:56](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/frontend/src/features/plan/__tests__/planExport.test.ts#L56) asserts `expect(entities.map(e => e.name)).toEqual(['STOCK', 'L1_STOCK', ...])`. Vitest test fails with `expected [ 'STOCK', 'WRAP_STOCK', … ] to deeply equal [ 'STOCK', 'L1_STOCK', … ]`. | Run `npx vitest run src/features/plan/__tests__/planExport.test.ts` | `frontend/src/features/plan/planPipeline.ts` or `planExport.test.ts` |
| **D6** | **FAIL** (Present) | Silent | In [view_graph.py:695-700](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/agent/view_graph.py#L695-L700), `synthesize_view_graphs_from_plan` filters `proposed_views` to nodes where `layer in ('L3', 'L4')` or `operation in ('join', 'calculated_column', 'fact', 'analytic_model')`. L1 wrapper datasets are omitted, so no internal graphical view is ever synthesized for wrappers. | Run `audit/repros/run_backend_repros.py` | `backend/app/agent/view_graph.py` |
| **D7** | **FAIL** (Present) | Data-Wrong | In [view_graph.py:556-589](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/agent/view_graph.py#L556-L589), when a fact node has 3+ inputs, `build_view_graph_for_proposed_node` creates exactly 1 Join node and attaches all 3 incoming source edges to it. In SAP Datasphere graphical views, a Join node is strictly a binary (2-input) operator; multi-table joins require a chained tree of binary joins. | Run `audit/repros/run_backend_repros.py` | `backend/app/agent/view_graph.py` |
| **D8** | **FAIL** (Present) | Data-Wrong | In [view_graph.py:721-728, 746-750, 782, 788-791, 800-802](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/agent/view_graph.py#L721-L728), `view_graph.py` contains hardcoded PO APT assumptions: `"ZIPOHDRAPT"`, `"V_PO_FINAL_DELIVERY_APT"`, `"CALC_PROCESSING_DAYS"`, `"DAYS_BETWEEN(CREATIONDATE, FINAL_DELIVERY_DATE) + 1"`, and fallback dummy columns `["ID", "CODE", "NAME", "VALUE"]`. When generic plans are synthesized, these heuristics contaminate the resulting graphs. | Run `audit/repros/run_backend_repros.py` | `backend/app/agent/view_graph.py` |
| **D9** | **PASS** (Not Present) | None | Suspected defect claimed `preparePlanPipeline` is not idempotent (`prepare(prepare(p)) != prepare(p)`). Verified empirically across raw plans, folded projection/filter plans, and intermediate L2 plans: in all cases, `prepare(prepare(p)) === prepare(p)`. | Run `npx --package=tsx tsx ../audit/repros/test_d9_idempotence.ts` | None (Claim refuted) |
| **D10** | **FAIL** (Present) | Silent | Structural divergence: frontend [planExport.ts:38-45](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/frontend/src/features/plan/planExport.ts#L38-L45) `buildPlanExportEntities` outputs base sources, `WRAP_` wrappers, fact, and model entities. In contrast, backend `view_graph.synthesize_view_graphs_from_plan` outputs only proposed views (L3/L4) and omits wrappers entirely. Lineage visualizer and exported CSN therefore represent different entity sets for the exact same plan. | Run `npx --package=tsx tsx ../audit/repros/run_frontend_repros.ts` | `frontend/src/features/plan/planExport.ts` & `backend/app/agent/view_graph.py` |
| **D11** | **FAIL** (Present) | Data-Wrong | In [plan_checker.py:82-86](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/agent/plan_checker.py#L82-L86), `check_plan` validates measures by checking if `m.column in available_columns`. For ratio/formula KPIs, the LLM sets `m.column` to an arithmetic formula string (`'Total_Net_Sales / Order_Count'`). `check_plan` rejects this as a missing column, failing plan generation with HTTP 422 across all retries. | Run `audit/repros/run_backend_repros.py` & verified in Task 2 live failure | `backend/app/agent/plan_checker.py` & `backend/app/agent/plan_models.py` |
| **D12** | **FAIL** (Present) | Data-Wrong | In [plan_checker.py:126-150](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/agent/plan_checker.py#L126-L150), when join keys are unqualified (bare column names as shown in `planner_system.md`), `default_input` for the right key is hardcoded to `node.inputs[1]`. If there are 3 inputs (`['L1_VBAK', 'L1_VBRP', 'L1_KNA1']`), any join condition for input 2 (KNA1) is checked against input 1 (VBRP), causing false validation failures. | Run `audit/repros/run_backend_repros.py` & Task 5 Case A | `backend/app/agent/plan_checker.py` |
| **D13** | **FAIL** (Present) | Data-Wrong | In [plan_checker.py:82-86](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/agent/plan_checker.py#L82-L86), `m.column` is not stripped of table qualifiers (`_strip_qualifier`). If the LLM generates a qualified measure column such as `'VBAK.NETWR'`, `check_plan` fails with `Measure references column 'VBAK.NETWR' which does not exist in any view` because `available_objects` stores unqualified names (`'NETWR'`). | Run `audit/repros/run_backend_repros.py` | `backend/app/agent/plan_checker.py` |
| **D14** | **FAIL** (Present) | Silent | In [main.py:38-41](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/backend/app/main.py#L38-L41), only `ws_router`, `plan_router`, and `lineage_router` are mounted. Routes `/api/v1/runs`, `/api/v1/canvas`, `/api/v1/connections`, `/api/v1/catalog`, `/api/v1/export` are not mounted. The frontend API client ([client.ts:3](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/frontend/src/api/client.ts#L3)) still points to `/api/v1`, causing immediate 404 errors when visiting `/runs`, `/canvas`, `/catalog`, or `/connections`. | Verified by AST reachability trace & HTTP probe | `backend/app/main.py` |
| **D15** | **FAIL** (Present) | Silent | In [ChatRequirementPage.tsx:322-327](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/frontend/src/pages/ChatRequirementPage.tsx#L322-L327), if `generatePlanApi` fails (e.g. HTTP 422 or network error), the `catch (apiErr)` block logs a `console.warn` and proceeds to `navigate('/plan')`. The user is silently redirected to the Plan visualizer showing the hardcoded fallback `SAMPLE_PO_PLAN` instead of seeing the error message. | Code inspection in `ChatRequirementPage.tsx` | `frontend/src/pages/ChatRequirementPage.tsx` |

---

## 6. LLM Plan Quality Scores and Observations (Task 5)

Each case was executed twice against the live Gemini provider via [audit/run_task5_quality.py](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/audit/run_task5_quality.py). Results were logged to [audit/task5_quality_results.json](file:///c:/Users/user/Desktop/DS-AgentForge/DS-AgentForge/audit/task5_quality_results.json).

### Evaluation Criteria (Scored 0–2 per criterion)
- **C1: Table & Column Integrity:** Does not invent columns or tables (0 = hallucinates, 1 = minor hallucination, 2 = strict adherence to metadata).
- **C2: Handling Ambiguity / Choices:** Transparently documents choices instead of silently guessing (0 = silent guess, 1 = partial documentation, 2 = explicit assumption/blocker).
- **C3: Gap Reporting:** Gaps placed in `assumptions` or `blockers` (0 = hidden, 1 = mentioned in text only, 2 = structured in `blockers`/`assumptions`).
- **C4: KPI Math & Ratio Construction:** Ratio KPIs built as FORMULA of sums vs row-level AVG (0 = invalid/broken, 1 = AVG of row ratio, 2 = SUM / SUM formula in model).
- **C5: Division Safety:** Divisions guarded against zero/null (0 = unguarded, 1 = partial guard, 2 = robust `CASE WHEN ... THEN 0`).
- **C6: Pipeline Structural Adherence:** Exactly one fact and one analytic model, correct layers (0 = violated, 1 = partial, 2 = strict adherence).
- **C7: Retry Behavior:** Responds intelligently to `plan_checker` error feedback (0 = ignores/loops, 1 = fixes partially, 2 = resolves error on retry).

### Quality Scorecard

| Criterion | Case A: Clean Sales KPI (Run 1 / Run 2) | Case B: Missing Column `DISCOUNT` (Run 1 / Run 2) | Case C: Ambiguous `NETWR` (VBAK vs VBRP) (Run 1 / Run 2) | Overall Observation |
|---|---|---|---|---|
| **C1: Column Integrity** | 2 / 2 | 2 / 2 | 2 / 2 | Model never hallucinated tables or non-existent columns from thin air. |
| **C2: Ambiguity Handling** | 2 / 2 | 2 / 2 | 2 / 2 | In Case C, Run 2 explicitly stated in assumptions: `Item-level net sales from VBRP.NETWR is used for calculating total net sales.` |
| **C3: Gap Reporting** | 2 / 2 | 2 / 2 | 2 / 2 | In Case B, Run 1 recorded: `Missing required column DISCOUNT in source metadata`. Run 2 recorded: `Column DISCOUNT is required ... but does not exist`. |
| **C4: Ratio Construction** | 0 / 0 | 0 / 0 | 1 / 1 | The model consistently attempts to place ratio formulas in `PlanMeasure.column` (`TotalNetSales / OrderCount`), which breaks `check_plan`. |
| **C5: Division Safety** | 0 / 0 | 0 / 0 | N/A | Division formulas generated (`(NETWR - DISCOUNT) / NETWR`) contained zero guards (`CASE WHEN ... THEN 0` was omitted). |
| **C6: Structural Adherence** | 1 / 1 | 2 / 2 | 1 / 2 | In Case A, the L4 analytic model was omitted from `nodes`. In Case C Run 2, both L3 and L4 were generated correctly. |
| **C7: Retry Behavior** | 1 / 1 | 2 / 2 | 1 / 2 | Model successfully adjusted join qualifiers and measure pointers on retry, but was repeatedly blocked by `check_plan` bugs (D11, D12). |
| **TOTAL SCORE (out of 14)** | **8 / 8** | **10 / 10** | **9 / 11** | Average score: **9.3 / 14** |

### Stability Assessment (Run 1 vs Run 2)
- **Case A Stability:** Consistent failure across both runs due to the 3-input join check bug (D12). Both runs generated functionally equivalent joins and measures.
- **Case B Stability:** High stability. Both runs identified `DISCOUNT` as missing and placed it into `blockers`. Run 1 stopped before adding calculated fields, whereas Run 2 added `(NETWR - DISCOUNT) / NETWR` to `calculated_fields` while flagging `blockers`.
- **Case C Stability:** Fragile stability. Run 1 produced bare join keys and failed all 3 retries due to D12. Run 2 generated fully qualified wrapper aliases (`L1_VBAK.KUNNR`, `L1_KNA1.KUNNR`), passing in 1 attempt.

---

## 7. Test Results (Task 6)

### Suite Summary
- **Backend (`pytest`):** 5 Failed, 67 Passed, 5 Warnings (72 total tests).
- **Frontend (`vitest`):** 2 Failed, 102 Passed (104 total tests in 10 test files).

### Detailed Breakdown of Failures

| Suite | Test Identifier | Error Message / Assertion | Root Cause | Classification |
|---|---|---|---|---|
| Backend | `tests/test_api.py::test_health_endpoint` | `Failed: async def functions are not natively supported. You need to install a suitable plugin for your async framework (e.g. pytest-asyncio)` | Test function is declared `async def` with `@pytest.mark.asyncio`, but `pytest-asyncio` plugin was not registered in the active test runner environment. | **Environment Problem** |
| Backend | `tests/test_api.py::test_root_endpoint` | `Failed: async def functions are not natively supported...` | Same as above. | **Environment Problem** |
| Backend | `tests/test_api_step4.py::test_post_plan_success` | `AssertionError: assert 3 == 4` at line 56: `assert len(data["plan"]["nodes"]) == 4` | Stale test assertion. The test expects 4 nodes (2 sources + 1 projection + 1 join), but the updated pipeline folds projection into wrapper, returning 3 nodes. | **Stale Test** |
| Backend | `tests/test_api_step4.py::test_get_plan_success` | `AssertionError: assert 3 == 4` at line 101: `assert len(data["plan"]["nodes"]) == 4` | Same as above. | **Stale Test** |
| Backend | `tests/test_validator.py::test_validator_clean_views` | `assert False is True` at line 33: `assert report["passed"] is True` | In dead module `backend/app/services/validator.py`, the validator requires mandatory metadata keys that the test payload omitted. | **Real Bug (in Dead Code)** |
| Frontend | `src/features/plan/__tests__/planExport.test.ts > buildPlanExportEntities` | `AssertionError: expected [ 'STOCK', 'WRAP_STOCK', … ] to deeply equal [ 'STOCK', 'L1_STOCK', … ]` at line 56 | Defect D5: Implementation generates `WRAP_` wrapper names, but test expects `L1_`. | **Real Bug** |
| Frontend | `src/features/plan/__tests__/planPipeline.test.ts > plan pipeline preparation` | `AssertionError: expected [ 'ORDER_ID', 'CUSTOMER_ID' ] to include 'STATUS'` at line 136 | Defect D1: `getRequiredNodeColumns` fails to lock columns used in the wrapper's own filter condition. | **Real Bug** |

---

## 8. What Was Verified by Running vs Only by Reading

### Verified by Running
1. Backend test suite execution (`pytest`): 5 failed, 67 passed, verified all error traces.
2. Frontend test suite execution (`vitest`): 2 failed, 102 passed, verified failure lines.
3. Live uvicorn backend on port 8000 and Vite frontend on port 3000 verified running and responding.
4. Live Gemini LLM provider connection verified via standalone invocation (`{"response": "OK"}`).
5. AST module reachability analysis verified 43 dead modules and 15 live modules via `audit/trace_reachability.py`.
6. Live scenario executed end to end (`POST /api/plan`, `POST /api/plan/revise`, `POST /api/lineage/synthesize`), recording real request and response payloads in `/audit/payloads/`.
7. Suspected defects D1, D5, D9, D10 verified via `audit/repros/run_frontend_repros.ts` and `audit/repros/test_d9_idempotence.ts`.
8. Backend defects D2, D6, D7, D8, D11, D12, D13 verified via `audit/repros/run_backend_repros.py`.
9. Task 5 plan quality evaluation executed 6 times against live Gemini provider via `audit/run_task5_quality.py`.

### Read from Code Only
1. Security settings and JWT expiration behavior in `backend/app/core/config.py`.
2. WebSocket subscriber connection drop handling in `backend/app/api/v1/websocket.py:33-41`.
3. Legacy LangGraph state transitions in dead module `backend/app/agent/states.py`.

---

## 9. What Could Not Be Checked and Why

1. **Live SAP Datasphere Deployment / CSN Import:** Could not test direct deployment of generated CSN JSON into a live SAP Datasphere tenant because tenant credentials and `ngdbc.jar` JDBC drivers are not configured in this local test environment.
2. **Old LangGraph Agent Pipeline (`/api/v1/runs`):** Could not test the old multi-step LangGraph workflow end to end because its database models (`Run`, `GraphNode`, `AuditLog`) require PostgreSQL with `pgvector`, which is not mounted or running in `app/main.py`.

---

## 10. Recommended Fix Order and Actionable Roadmap

The table below outlines which defects are present and the precise order in which the repository owner should fix them.

| Priority | Defect | Status | Impact / Rationale | Files Needing Fix |
|---|---|---|---|---|
| **1** | **D11** | **FAIL** (Present) | **High (Blocker):** `check_plan` rejects ratio KPI formulas in `PlanMeasure.column`, causing `POST /api/plan` to fail with HTTP 422 for any requirement requesting a ratio KPI. Allow formula expressions when aggregation is `FORMULA` or `RATIO`, or add a dedicated `formula` field to `PlanMeasure`. | `backend/app/agent/plan_checker.py`, `backend/app/agent/plan_models.py`, `backend/app/agent/prompts/planner_system.md` |
| **2** | **D12** | **FAIL** (Present) | **High (Data-Wrong):** Unqualified join keys on 3+ input fact nodes default right key to `node.inputs[1]`, causing false join validation failures. Look up bare keys across all candidate inputs. | `backend/app/agent/plan_checker.py` |
| **3** | **D13** | **FAIL** (Present) | **High (Data-Wrong):** Measure column validation fails when qualified with table names (`VBAK.NETWR`). Apply `_strip_qualifier` before checking column existence. | `backend/app/agent/plan_checker.py` |
| **4** | **D1** | **FAIL** (Present) | **High (Data-Wrong):** Wrapper filter columns and calculated field formula columns are stripped from wrappers because `getRequiredNodeColumns` does not lock them. | `frontend/src/features/plan/planPipeline.ts` |
| **5** | **D2** | **FAIL** (Present) | **High (Data-Loss):** `PlanNode` drops `is_wrapper`, `is_projection_enabled`, and `is_filter_enabled` on Pydantic validation, wiping wrapper filter configurations on round trip. | `backend/app/agent/plan_models.py` |
| **6** | **D3** | **FAIL** (Present) | **Medium (Data-Loss):** Quick AI revise sends only `{ plan_id, instruction }`, causing backend to revise a stale plan and overwrite client wrapper edits. Frontend must send current client plan or sync manual edits. | `frontend/src/features/plan/PlanPage.tsx`, `backend/app/api/v1/plan_routes.py` |
| **7** | **D4** | **FAIL** (Present) | **Medium (Silent):** "Accept Plan" does zero schema validation before transitioning to Lineage. Add client-side or backend validation gate before navigation. | `frontend/src/features/plan/PlanPage.tsx` |
| **8** | **D7** | **FAIL** (Present) | **Medium (Data-Wrong):** 3-way joins are dumped into a single 3-input Join node in `view_graph.py` instead of building a chained binary join tree. | `backend/app/agent/view_graph.py` |
| **9** | **D8** | **FAIL** (Present) | **Medium (Data-Wrong):** Remove hardcoded PO APT assumptions (`ZIPOHDRAPT`, `CALC_PROCESSING_DAYS`, fallback dummy columns) from `view_graph.py`. | `backend/app/agent/view_graph.py` |
| **10** | **D6** | **FAIL** (Present) | **Medium (Cosmetic):** `synthesize_view_graphs_from_plan` omits wrappers from proposed views. Synthesize internal view graphs for L1 wrapper datasets. | `backend/app/agent/view_graph.py` |
| **11** | **D10** | **FAIL** (Present) | **Medium (Divergence):** Align entity models between `planExport.buildPlanExportEntities` and `view_graph.synthesize_view_graphs_from_plan`. | `frontend/src/features/plan/planExport.ts`, `backend/app/agent/view_graph.py` |
| **12** | **D5** | **FAIL** (Present) | **Low (Test-Breakage):** Standardize wrapper naming convention across frontend code and tests (`WRAP_` vs `L1_`). | `frontend/src/features/plan/planPipeline.ts`, `frontend/src/features/plan/__tests__/planExport.test.ts` |
| **13** | **D15** | **FAIL** (Present) | **Low (UX Silent Bug):** `ChatRequirementPage.tsx` catch block must show the user an error alert instead of silently navigating to `/plan` with a mock plan. | `frontend/src/pages/ChatRequirementPage.tsx` |
| **14** | **D14** | **FAIL** (Present) | **Low (Clean-Up):** Mount missing routers in `main.py` or remove dead frontend navigation links (`/runs`, `/canvas`, `/catalog`). | `backend/app/main.py`, `frontend/src/App.tsx` |
| **-** | **D9** | **PASS** (Not Present) | **No Action Required:** `preparePlanPipeline` was verified to be strictly idempotent. | None |
