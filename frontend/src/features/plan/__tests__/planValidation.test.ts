import { describe, expect, it } from 'vitest';
import { CompressedEntity } from '../../../utils/schemaCompressor';
import { Plan } from '../types';
import { validatePlanForExport } from '../planValidation';
import { preparePlanPipeline } from '../planPipeline';

describe('validatePlanForExport', () => {
  const sourceEntities: CompressedEntity[] = [
    {
      name: 'ORDERS',
      kind: 'table',
      sources: [],
      fields: [
        { name: 'ORDER_ID', data_type: 'cds.Integer', is_key: true, label: 'Order ID', semantic_role: 'key' },
        { name: 'CUSTOMER_ID', data_type: 'cds.Integer', is_key: false, label: 'Customer ID', semantic_role: 'dimension' },
        { name: 'AMOUNT', data_type: 'cds.Decimal', is_key: false, label: 'Amount', semantic_role: 'measure' },
      ],
    },
  ];

  const validPlan: Plan = {
    analysis: ['Valid test plan'],
    nodes: [
      {
        id: 'ORDERS',
        operation: 'source',
        inputs: [],
        columns: ['ORDER_ID', 'CUSTOMER_ID', 'AMOUNT'],
        layer: 'L0',
      },
      {
        id: 'MN_WRAP_ORDERS',
        operation: 'projection',
        inputs: ['ORDERS'],
        columns: ['ORDER_ID', 'CUSTOMER_ID', 'AMOUNT'],
        is_wrapper: true,
        layer: 'L1',
      },
      {
        id: 'L3_FACT',
        operation: 'join',
        inputs: ['MN_WRAP_ORDERS'],
        columns: ['ORDER_ID', 'CUSTOMER_ID', 'AMOUNT'],
        calculated_fields: [
          { name: 'CALC_AMOUNT', formula: 'AMOUNT * 1.1', label: 'Calc Amount' },
        ],
        details: {},
        layer: 'L3',
      },
      {
        id: 'L4_MODEL',
        operation: 'analytic_model',
        inputs: ['L3_FACT'],
        columns: ['ORDER_ID', 'CUSTOMER_ID'],
        layer: 'L4',
      },
    ],
    measures: [
      { name: 'TOTAL_AMOUNT', aggregation: 'SUM', column: 'CALC_AMOUNT' },
    ],
    dimensions: ['ORDER_ID', 'CUSTOMER_ID'],
    assumptions: [],
    blockers: [],
  };

  it('validates a compliant plan with zero errors', () => {
    const result = validatePlanForExport(validPlan, sourceEntities);
    expect(result.errors).toEqual([]);
  });

  it('rejects forbidden delta entities in nodes and inputs', () => {
    const deltaPlan: Plan = {
      ...validPlan,
      nodes: [
        ...validPlan.nodes,
        {
          id: 'ORDERS_DELTA',
          operation: 'source',
          inputs: [],
          columns: ['ORDER_ID'],
          layer: 'L0',
        },
      ],
    };
    const result = validatePlanForExport(deltaPlan, sourceEntities);
    expect(result.errors.some((e) => e.includes("references forbidden delta replication entity"))).toBe(true);
  });

  it('rejects plans missing an analytic model or fact node', () => {
    const noModelPlan: Plan = {
      ...validPlan,
      nodes: validPlan.nodes.filter((n) => n.layer !== 'L4'),
    };
    const result = validatePlanForExport(noModelPlan, sourceEntities);
    expect(result.errors).toContain('Plan must contain exactly one analytic_model node, got 0.');
  });

  it('rejects fact nodes whose inputs are not wrappers', () => {
    const rawInputPlan: Plan = {
      ...validPlan,
      nodes: validPlan.nodes.map((n) =>
        n.id === 'L3_FACT' ? { ...n, inputs: ['ORDERS'] } : n
      ),
    };
    const result = validatePlanForExport(rawInputPlan, sourceEntities);
    expect(result.errors).toContain("Fact node 'L3_FACT' input 'ORDERS' is not a wrapper node.");
  });

  it('rejects wrapper with filter enabled but empty condition', () => {
    const emptyCondPlan: Plan = {
      ...validPlan,
      nodes: validPlan.nodes.map((n) =>
        n.id === 'MN_WRAP_ORDERS'
          ? { ...n, is_filter_enabled: true, details: { condition: '   ' } }
          : n
      ),
    };
    const result = validatePlanForExport(emptyCondPlan, sourceEntities);
    expect(result.errors).toContain("Wrapper 'MN_WRAP_ORDERS' has filter enabled but condition is empty.");
  });

  it('rejects wrapper filter referencing missing column naming node and column', () => {
    const missingColPlan: Plan = {
      ...validPlan,
      nodes: validPlan.nodes.map((n) =>
        n.id === 'MN_WRAP_ORDERS'
          ? { ...n, details: { condition: "GHOST_COL = 'X'" } }
          : n
      ),
    };
    const result = validatePlanForExport(missingColPlan, sourceEntities);
    expect(result.errors).toContain(
      "Wrapper 'MN_WRAP_ORDERS' filter condition references column 'GHOST_COL' which is missing from wrapper."
    );
  });

  it('rejects fact calculated field referencing missing column', () => {
    const missingCalcColPlan: Plan = {
      ...validPlan,
      nodes: validPlan.nodes.map((n) =>
        n.id === 'L3_FACT'
          ? {
              ...n,
              calculated_fields: [{ name: 'NEW_CALC', formula: 'NON_EXISTENT * 2' }],
            }
          : n
      ),
    };
    const result = validatePlanForExport(missingCalcColPlan, sourceEntities);
    expect(result.errors).toContain(
      "Fact node 'L3_FACT' calculated field 'NEW_CALC' formula references column 'NON_EXISTENT' which is missing."
    );
  });

  it('rejects measure referencing missing column from fact node', () => {
    const missingMeasureColPlan: Plan = {
      ...validPlan,
      measures: [{ name: 'BAD_MEASURE', aggregation: 'SUM', column: 'MISSING_MEASURE_COL' }],
    };
    const result = validatePlanForExport(missingMeasureColPlan, sourceEntities);
    expect(result.errors).toContain(
      "Measure 'BAD_MEASURE' references column 'MISSING_MEASURE_COL' which is missing from fact node 'L3_FACT'."
    );
  });

  it('rejects dimension missing from fact node', () => {
    const missingDimPlan: Plan = {
      ...validPlan,
      dimensions: ['MISSING_DIMENSION'],
    };
    const result = validatePlanForExport(missingDimPlan, sourceEntities);
    expect(result.errors).toContain("Dimension 'MISSING_DIMENSION' is missing from fact node 'L3_FACT'.");
  });
});

describe('wrapper rewriting in preparePlanPipeline', () => {
  const sourceEntities: CompressedEntity[] = [
    {
      name: 'ORDERS',
      kind: 'table',
      sources: [],
      fields: [
        { name: 'ORDER_ID', data_type: 'cds.Integer', is_key: true, label: 'Order ID', semantic_role: 'key' },
        { name: 'CUSTOMER_ID', data_type: 'cds.Integer', is_key: false, label: 'Customer ID', semantic_role: 'dimension' },
        { name: 'NET_AMOUNT', data_type: 'cds.Decimal', is_key: false, label: 'Net Amount', semantic_role: 'measure' },
      ],
    },
  ];

  it('rewires join keys, calculated_fields, measures, and dimensions from L1/WRAP to MN_WRAP', () => {
    const planWithL1: Plan = {
      analysis: [],
      nodes: [
        {
          id: 'ORDERS',
          operation: 'source',
          inputs: [],
          columns: ['ORDER_ID', 'CUSTOMER_ID', 'NET_AMOUNT'],
          layer: 'L0',
        },
        {
          id: 'L3_FACT',
          operation: 'join',
          inputs: ['ORDERS'],
          columns: ['ORDER_ID', 'CUSTOMER_ID', 'NET_AMOUNT', 'DISCOUNTED'],
          calculated_fields: [
            { name: 'DISCOUNTED', formula: 'L1_ORDERS.NET_AMOUNT * 0.9' },
          ],
          details: {
            on: [['L1_ORDERS.CUSTOMER_ID', 'L1_ORDERS.CUSTOMER_ID']],
          },
          layer: 'L3',
        },
        {
          id: 'L4_MODEL',
          operation: 'analytic_model',
          inputs: ['L3_FACT'],
          columns: ['ORDER_ID'],
          layer: 'L4',
        },
      ],
      measures: [
        { name: 'TOTAL_NET', aggregation: 'SUM', column: 'L1_ORDERS.NET_AMOUNT' },
        { name: 'RATIO', aggregation: 'FORMULA', formula: 'L1_ORDERS.NET_AMOUNT / 10' },
      ],
      dimensions: ['L1_ORDERS.CUSTOMER_ID'],
      assumptions: [],
      blockers: [],
    };

    const prepared = preparePlanPipeline(planWithL1, sourceEntities);
    const fact = prepared.nodes.find((n) => n.id === 'L3_FACT');

    // Fact inputs rewired to MN_WRAP_ORDERS
    expect(fact?.inputs).toContain('MN_WRAP_ORDERS');
    // Join on rewired
    expect(fact?.details.on).toEqual([['MN_WRAP_ORDERS.CUSTOMER_ID', 'MN_WRAP_ORDERS.CUSTOMER_ID']]);
    // Calculated field formula rewired
    expect(fact?.calculated_fields?.[0].formula).toBe('MN_WRAP_ORDERS.NET_AMOUNT * 0.9');
    // Measures rewired
    expect(prepared.measures?.[0].column).toBe('MN_WRAP_ORDERS.NET_AMOUNT');
    expect(prepared.measures?.[1].formula).toBe('MN_WRAP_ORDERS.NET_AMOUNT / 10');
    // Dimensions rewired
    expect(prepared.dimensions).toEqual(['MN_WRAP_ORDERS.CUSTOMER_ID']);
  });
});
