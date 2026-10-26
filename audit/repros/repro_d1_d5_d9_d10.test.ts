import { describe, it, expect } from 'vitest';
import { getRequiredNodeColumns, preparePlanPipeline } from '../../frontend/src/features/plan/planPipeline';
import { buildPlanExportEntities } from '../../frontend/src/features/plan/planExport';
import { Plan, PlanNode } from '../../frontend/src/features/plan/types';
import { CompressedEntity } from '../../frontend/src/utils/schemaCompressor';

describe('Audit Reproductions for D1, D5, D9, D10', () => {
  // D1: getRequiredNodeColumns fails to lock columns used in calculated_fields[].formula,
  // wrapper's own filter condition, plan.measures, plan.dimensions.
  it('D1 REPRO: getRequiredNodeColumns omits wrapper filter column and calculated_fields formula columns', () => {
    const plan: Plan = {
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

    const wrapperNode = plan.nodes[0];
    const requiredCols = getRequiredNodeColumns(wrapperNode, plan);

    // Defect check:
    // 'STATUS' is used in WRAP_ORDERS filter condition and plan.dimensions.
    // 'NET_AMOUNT' is used in L3_FACT calculated_fields[0].formula and plan.measures.
    console.log('D1 actual requiredCols:', requiredCols);
    const hasStatus = requiredCols.includes('STATUS');
    const hasNetAmount = requiredCols.includes('NET_AMOUNT');

    // If defect is present, hasStatus is FALSE and hasNetAmount is FALSE!
    expect(hasStatus).toBe(false); // Confirms D1 defect: wrapper filter & dimension column NOT locked!
    expect(hasNetAmount).toBe(false); // Confirms D1 defect: calculated_fields formula & measure column NOT locked!
  });

  // D5: Wrapper naming mismatch (WRAP_ vs L1_)
  it('D5 REPRO: planPipeline creates WRAP_ prefix while tests/CSN export expect L1_ prefix', () => {
    const plan: Plan = {
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
    const prepared = preparePlanPipeline(plan, []);
    const wrapper = prepared.nodes.find((n) => n.inputs.includes('ORDERS') && n.id !== 'ORDERS');
    console.log('D5 generated wrapper ID:', wrapper?.id);
    expect(wrapper?.id).toBe('WRAP_ORDERS'); // implementation uses WRAP_
    // In planExport.test.ts:56, it asserts: expect(entities.map(e => e.name)).toEqual(['ORDERS', 'L1_ORDERS', ...])
    // Which causes test failure because wrapper is WRAP_ORDERS, not L1_ORDERS.
  });

  // D9: preparePlanPipeline is NOT idempotent: prepare(prepare(p)) != prepare(p)
  it('D9 REPRO: preparePlanPipeline is not idempotent', () => {
    const source: CompressedEntity = {
      name: 'ORDERS',
      kind: 'table',
      sources: [],
      fields: [
        { name: 'ORDER_ID', data_type: 'NVARCHAR', is_key: true, label: 'Order', semantic_role: 'key' },
        { name: 'CUSTOMER_ID', data_type: 'NVARCHAR', is_key: false, label: 'Customer', semantic_role: 'dimension' },
      ],
    };

    const initialPlan: Plan = {
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

    const run1 = preparePlanPipeline(initialPlan, [source]);
    const run2 = preparePlanPipeline(run1, [source]);

    // Check if run1 and run2 are identical
    const run1Json = JSON.stringify(run1);
    const run2Json = JSON.stringify(run2);

    console.log('D9 run1 nodes:', run1.nodes.map((n) => n.id));
    console.log('D9 run2 nodes:', run2.nodes.map((n) => n.id));
    console.log('D9 run1 vs run2 equal:', run1Json === run2Json);

    // If not idempotent, compare on conditions or structure
    expect(run1Json === run2Json).toBe(false); // Confirms D9 defect: prepare is NOT idempotent!
  });

  // D10: Comparison between lineage drawing (backend view_graph) and frontend planExport
  it('D10 REPRO: planExport vs backend view_graph discrepancy', () => {
    const source: CompressedEntity = {
      name: 'ORDERS',
      kind: 'table',
      sources: [],
      fields: [
        { name: 'ORDER_ID', data_type: 'NVARCHAR', is_key: true, label: 'Order', semantic_role: 'key' },
        { name: 'AMOUNT', data_type: 'DECIMAL', is_key: false, label: 'Amount', semantic_role: 'measure' },
      ],
    };
    const plan: Plan = {
      analysis: ['Test'],
      nodes: [
        {
          id: 'ORDERS',
          operation: 'source',
          inputs: [],
          columns: ['ORDER_ID', 'AMOUNT'],
          details: {},
          layer: 'L0',
        },
        {
          id: 'L3_FACT',
          operation: 'join',
          inputs: ['ORDERS'],
          columns: ['ORDER_ID', 'AMOUNT'],
          calculated_fields: [{ name: 'TAX', formula: 'AMOUNT * 0.1', label: 'Tax', data_type: 'DECIMAL' }],
          details: {},
          layer: 'L3',
        },
      ],
      measures: [{ name: 'TOTAL_AMOUNT', aggregation: 'SUM', column: 'AMOUNT' }],
      dimensions: ['ORDER_ID'],
      assumptions: [],
      blockers: [],
    };

    const exportEntities = buildPlanExportEntities(plan, [source]);
    console.log('D10 export entities:', exportEntities.map((e) => e.name));
    // Frontend exports: ORDERS (source), WRAP_ORDERS (wrapper), L3_FACT (fact)
    expect(exportEntities.map((e) => e.name)).toContain('WRAP_ORDERS');
    // Whereas backend view_graph.synthesize_view_graphs_from_plan NEVER outputs wrappers!
  });
});
