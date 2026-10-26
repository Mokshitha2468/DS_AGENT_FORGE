import { getRequiredNodeColumns, preparePlanPipeline } from '../../frontend/src/features/plan/planPipeline';
import { buildPlanExportEntities } from '../../frontend/src/features/plan/planExport';
import { Plan } from '../../frontend/src/features/plan/types';
import { CompressedEntity } from '../../frontend/src/utils/schemaCompressor';

console.log("================================================================================");
console.log("REPRODUCTION RESULTS FOR FRONTEND DEFECTS (D1, D5, D9, D10)");
console.log("================================================================================");

// ── D1 REPRO ──────────────────────────────────────────────────────────────────
console.log("\n[D1] getRequiredNodeColumns omits wrapper filter & formula columns:");
const planD1: Plan = {
  analysis: ['Test'],
  nodes: [
    {
      id: 'WRAP_ORDERS',
      operation: 'projection',
      inputs: ['ORDERS'],
      columns: ['ORDER_ID', 'STATUS', 'NET_AMOUNT', 'CUSTOMER_ID'],
      details: { condition: "STATUS = 'Completed'" },
      is_wrapper: true,
      layer: 'L1',
    },
    {
      id: 'L3_FACT',
      operation: 'join',
      inputs: ['WRAP_ORDERS'],
      columns: ['ORDER_ID', 'CUSTOMER_ID', 'DISCOUNTED_TOTAL'],
      calculated_fields: [
        {
          name: 'DISCOUNTED_TOTAL',
          formula: 'NET_AMOUNT * 0.9',
          label: 'Discounted Total',
          data_type: 'DECIMAL(15,2)',
        },
      ],
      details: {},
      layer: 'L3',
    },
  ],
  measures: [{ name: 'Total_Sales', aggregation: 'SUM', column: 'NET_AMOUNT' }],
  dimensions: ['STATUS'],
  assumptions: [],
  blockers: [],
};

const wrapperNode = planD1.nodes[0];
const requiredCols = getRequiredNodeColumns(wrapperNode, planD1);
console.log("Wrapper columns:", wrapperNode.columns);
console.log("Required columns returned by getRequiredNodeColumns:", requiredCols);
console.log("Is wrapper filter column 'STATUS' in requiredCols?", requiredCols.includes('STATUS'));
console.log("Is calculated_fields formula column 'NET_AMOUNT' in requiredCols?", requiredCols.includes('NET_AMOUNT'));
const d1Failed = !requiredCols.includes('STATUS') && !requiredCols.includes('NET_AMOUNT');
console.log("--> D1 VERDICT:", d1Failed ? "FAIL (Defect PRESENT)" : "PASS (Defect NOT present)");

// ── D5 REPRO ──────────────────────────────────────────────────────────────────
console.log("\n[D5] Wrapper naming inconsistency between code and tests:");
const planD5: Plan = {
  analysis: ['Test'],
  nodes: [
    {
      id: 'ORDERS',
      operation: 'source',
      inputs: [],
      columns: ['ORDER_ID', 'CUSTOMER_ID'],
      details: {},
      layer: 'L0',
    },
  ],
  measures: [],
  dimensions: [],
  assumptions: [],
  blockers: [],
};
const preparedD5 = preparePlanPipeline(planD5, []);
const wrapperD5 = preparedD5.nodes.find((n) => n.inputs.includes('ORDERS') && n.id !== 'ORDERS');
console.log("preparePlanPipeline generates wrapper ID:", wrapperD5?.id);
console.log("planExport.test.ts:56 expects wrapper ID: 'L1_ORDERS' (or 'L1_STOCK')");
const d5Failed = wrapperD5?.id.startsWith('WRAP_');
console.log("--> D5 VERDICT:", d5Failed ? "FAIL (Defect PRESENT - WRAP_ vs L1_ mismatch)" : "PASS");

// ── D9 REPRO ──────────────────────────────────────────────────────────────────
console.log("\n[D9] preparePlanPipeline idempotency test: prepare(prepare(p)) == prepare(p):");
const sourceD9: CompressedEntity = {
  name: 'ORDERS',
  kind: 'table',
  sources: [],
  fields: [
    { name: 'ORDER_ID', data_type: 'NVARCHAR', is_key: true, label: 'Order', semantic_role: 'key' },
    { name: 'CUSTOMER_ID', data_type: 'NVARCHAR', is_key: false, label: 'Customer', semantic_role: 'dimension' },
  ],
};

const initialPlanD9: Plan = {
  analysis: ['Test'],
  nodes: [
    {
      id: 'L2_FILTER',
      operation: 'filter',
      inputs: ['ORDERS'],
      columns: ['ORDER_ID', 'CUSTOMER_ID'],
      details: { condition: "CUSTOMER_ID = 'C1'" },
      layer: 'L2',
    },
    {
      id: 'L3_FACT',
      operation: 'join',
      inputs: ['L2_FILTER'],
      columns: ['ORDER_ID', 'CUSTOMER_ID'],
      details: { on: [['L2_FILTER.CUSTOMER_ID', 'CUST.CUSTOMER_ID']] },
      layer: 'L3',
    },
  ],
  measures: [],
  dimensions: [],
  assumptions: [],
  blockers: [],
};

const run1 = preparePlanPipeline(initialPlanD9, [sourceD9]);
const run2 = preparePlanPipeline(run1, [sourceD9]);
const run1Json = JSON.stringify(run1);
const run2Json = JSON.stringify(run2);
console.log("Run 1 nodes:", run1.nodes.map(n => n.id));
console.log("Run 2 nodes:", run2.nodes.map(n => n.id));
console.log("Run 1 join on:", JSON.stringify(run1.nodes.find(n => n.id === 'L3_FACT')?.details.on));
console.log("Run 2 join on:", JSON.stringify(run2.nodes.find(n => n.id === 'L3_FACT')?.details.on));
console.log("Is run1 strictly equal to run2?", run1Json === run2Json);
const d9Failed = run1Json !== run2Json;
console.log("--> D9 VERDICT:", d9Failed ? "FAIL (Defect PRESENT - not idempotent)" : "PASS");

// ── D10 REPRO ─────────────────────────────────────────────────────────────────
console.log("\n[D10] Lineage drawing (backend view_graph) vs frontend planExport.buildPlanExportEntities:");
const sourceD10: CompressedEntity = {
  name: 'VBAK',
  kind: 'table',
  sources: [],
  fields: [
    { name: 'VBELN', data_type: 'NVARCHAR', is_key: true, label: 'Order', semantic_role: 'key' },
    { name: 'NETWR', data_type: 'DECIMAL', is_key: false, label: 'Amount', semantic_role: 'measure' },
  ],
};
const planD10: Plan = {
  analysis: ['Test'],
  nodes: [
    {
      id: 'VBAK',
      operation: 'source',
      inputs: [],
      columns: ['VBELN', 'NETWR'],
      details: {},
      layer: 'L0',
    },
    {
      id: 'L3_FACT_SALES',
      operation: 'join',
      inputs: ['VBAK'],
      columns: ['VBELN', 'NETWR', 'ORDER_COUNT'],
      calculated_fields: [{ name: 'ORDER_COUNT', formula: '1', label: 'Count', data_type: 'INTEGER' }],
      details: {},
      layer: 'L3',
    },
  ],
  measures: [{ name: 'TOTAL_SALES', aggregation: 'SUM', column: 'NETWR' }],
  dimensions: ['VBELN'],
  assumptions: [],
  blockers: [],
};

const exportEntities = buildPlanExportEntities(planD10, [sourceD10]);
console.log("planExport entities:", exportEntities.map(e => e.name));
console.log("Notice planExport creates 'WRAP_VBAK', whereas backend view_graph synthesize creates only proposed views (L3/L4) and NEVER wraps!");
const d10Failed = exportEntities.some(e => e.name.startsWith('WRAP_'));
console.log("--> D10 VERDICT:", d10Failed ? "FAIL (Defect PRESENT - structural divergence)" : "PASS");
