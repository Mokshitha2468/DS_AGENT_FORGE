import { preparePlanPipeline } from '../../frontend/src/features/plan/planPipeline';
import { Plan } from '../../frontend/src/features/plan/types';
import { CompressedEntity } from '../../frontend/src/utils/schemaCompressor';

const sources: CompressedEntity[] = [
  {
    name: 'VBAK',
    kind: 'table',
    sources: [],
    fields: [
      { name: 'VBELN', data_type: 'NVARCHAR', is_key: true, label: 'Order', semantic_role: 'key' },
      { name: 'KUNNR', data_type: 'NVARCHAR', is_key: false, label: 'Customer', semantic_role: 'dimension' },
      { name: 'STATUS', data_type: 'NVARCHAR', is_key: false, label: 'Status', semantic_role: 'dimension' },
    ],
  },
  {
    name: 'KNA1',
    kind: 'table',
    sources: [],
    fields: [
      { name: 'KUNNR', data_type: 'NVARCHAR', is_key: true, label: 'Customer', semantic_role: 'key' },
      { name: 'LAND1', data_type: 'NVARCHAR', is_key: false, label: 'Country', semantic_role: 'dimension' },
    ],
  },
];

// Test case 1: Raw LLM plan without wrappers
const plan1: Plan = {
  analysis: ['Raw plan'],
  nodes: [
    { id: 'VBAK', operation: 'source', inputs: [], columns: ['VBELN', 'KUNNR', 'STATUS'], details: {}, layer: 'L0' },
    { id: 'KNA1', operation: 'source', inputs: [], columns: ['KUNNR', 'LAND1'], details: {}, layer: 'L0' },
    { id: 'L3_FACT', operation: 'join', inputs: ['VBAK', 'KNA1'], columns: ['VBELN', 'LAND1'], details: { on: [['VBAK.KUNNR', 'KNA1.KUNNR']] }, layer: 'L3' },
  ],
  measures: [],
  dimensions: [],
  assumptions: [],
  blockers: [],
};

// Test case 2: Plan with folded projection and filter
const plan2: Plan = {
  analysis: ['Folded plan'],
  nodes: [
    { id: 'VBAK', operation: 'source', inputs: [], columns: ['VBELN', 'KUNNR', 'STATUS'], details: {}, layer: 'L0' },
    { id: 'L1_PROJ', operation: 'projection', inputs: ['VBAK'], columns: ['VBELN', 'KUNNR'], details: {}, layer: 'L1' },
    { id: 'L1_FILT', operation: 'filter', inputs: ['VBAK'], columns: ['VBELN', 'KUNNR'], details: { condition: "STATUS = 'Active'" }, layer: 'L1' },
    { id: 'L3_FACT', operation: 'join', inputs: ['L1_PROJ', 'L1_FILT'], columns: ['VBELN'], details: { on: [['L1_PROJ.VBELN', 'L1_FILT.VBELN']] }, layer: 'L3' },
  ],
  measures: [],
  dimensions: [],
  assumptions: [],
  blockers: [],
};

// Test case 3: Plan with intermediate L2 view
const plan3: Plan = {
  analysis: ['L2 intermediate plan'],
  nodes: [
    { id: 'VBAK', operation: 'source', inputs: [], columns: ['VBELN', 'KUNNR'], details: {}, layer: 'L0' },
    { id: 'L2_INTERMEDIATE', operation: 'projection', inputs: ['VBAK'], columns: ['VBELN', 'KUNNR'], details: { condition: 'VBELN IS NOT NULL' }, is_existing: true, layer: 'L2' },
    { id: 'L3_FACT', operation: 'join', inputs: ['L2_INTERMEDIATE'], columns: ['VBELN'], details: {}, layer: 'L3' },
  ],
  measures: [],
  dimensions: [],
  assumptions: [],
  blockers: [],
};

const testCases = [
  { name: 'Case 1 (raw plan)', plan: plan1 },
  { name: 'Case 2 (folded projection & filter)', plan: plan2 },
  { name: 'Case 3 (L2 intermediate)', plan: plan3 },
];

let allIdempotent = true;
for (const tc of testCases) {
  const p1 = preparePlanPipeline(tc.plan, sources);
  const p2 = preparePlanPipeline(p1, sources);
  const s1 = JSON.stringify(p1);
  const s2 = JSON.stringify(p2);
  const eq = s1 === s2;
  console.log(`${tc.name} idempotent?`, eq);
  if (!eq) {
    allIdempotent = false;
    console.log('Diff P1 nodes:', p1.nodes.map(n => n.id));
    console.log('Diff P2 nodes:', p2.nodes.map(n => n.id));
  }
}

console.log('Overall D9 Idempotence result:', allIdempotent ? 'PASS (Idempotent - Defect NOT present)' : 'FAIL (Defect PRESENT)');
