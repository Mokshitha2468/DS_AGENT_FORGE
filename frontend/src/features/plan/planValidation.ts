// frontend/src/features/plan/planValidation.ts
// Client-side deterministic validation mirroring backend plan_checker rules.

import { CompressedEntity, isDeltaEntityName } from '../../utils/schemaCompressor';
import { Plan, PlanNode } from './types';

export interface PlanValidationResult {
  errors: string[];
  warnings: string[];
}

const SQL_KEYWORDS = new Set([
  'CASE', 'WHEN', 'THEN', 'ELSE', 'END', 'OR', 'AND', 'NOT',
  'IS', 'NULL', 'TRUE', 'FALSE',
]);

export function validatePlanForExport(
  plan: Plan,
  sourceEntities: CompressedEntity[] = [],
): PlanValidationResult {
  const errors: string[] = [];
  const warnings: string[] = [];

  // 1. Delta entities check
  for (const node of plan.nodes) {
    if (isDeltaEntityName(node.id)) {
      errors.push(`Node '${node.id}' references forbidden delta replication entity.`);
    }
    for (const inp of node.inputs) {
      if (isDeltaEntityName(inp)) {
        errors.push(`Node '${node.id}' references input '${inp}' which is a forbidden delta replication entity.`);
      }
    }
  }

  // 2. Topology rules: exactly 1 fact and 1 analytic model
  const factNodes = plan.nodes.filter(
    (n) => n.layer === 'L3' || n.operation === 'fact' || (n.operation === 'join' && !['L1', 'L2'].includes(n.layer || '')),
  );
  if (factNodes.length !== 1) {
    errors.push(`Plan must contain exactly one fact node, got ${factNodes.length}.`);
  }
  const factNode = factNodes.length === 1 ? factNodes[0] : null;

  const modelNodes = plan.nodes.filter(
    (n) => n.layer === 'L4' || n.operation === 'analytic_model',
  );
  if (modelNodes.length !== 1) {
    errors.push(`Plan must contain exactly one analytic_model node, got ${modelNodes.length}.`);
  }
  const modelNode = modelNodes.length === 1 ? modelNodes[0] : null;

  if (factNode && modelNode) {
    if (!modelNode.inputs.includes(factNode.id)) {
      errors.push(`Analytic model '${modelNode.id}' must take fact node '${factNode.id}' as input.`);
    }
  }

  // 3. Fact inputs are wrappers
  if (factNode) {
    for (const inp of factNode.inputs) {
      const inpNode = plan.nodes.find((n) => n.id === inp);
      const isWrap =
        (inpNode && inpNode.is_wrapper) ||
        inp.startsWith('MN_WRAP_') ||
        inp.startsWith('WRAP_') ||
        inp.startsWith('L1_');
      if (!isWrap) {
        errors.push(`Fact node '${factNode.id}' input '${inp}' is not a wrapper node.`);
      }
    }
  }

  // 4. Source with zero or more than one wrapper (for sources used in the plan)
  const sourceNames = new Set<string>();
  for (const n of plan.nodes) {
    if (n.operation?.toLowerCase() === 'source' || n.layer === 'L0') {
      sourceNames.add(n.id);
    }
    const isWrap = n.is_wrapper || n.id.startsWith('MN_WRAP_') || n.id.startsWith('WRAP_') || n.id.startsWith('L1_');
    if (isWrap) {
      for (const inp of n.inputs) {
        if (!inp.startsWith('MN_WRAP_') && !inp.startsWith('WRAP_') && !inp.startsWith('L1_')) {
          sourceNames.add(inp);
        }
      }
    }
  }

  const cleanSourceNames = Array.from(sourceNames).filter((s) => !isDeltaEntityName(s));

  if (sourceEntities.length > 0) {
    const knownSources = new Set(sourceEntities.map((e) => e.name));
    for (const s of cleanSourceNames) {
      if (!knownSources.has(s)) {
        errors.push(`Source entity '${s}' does not exist in metadata.`);
      }
    }
  }

  for (const source of cleanSourceNames.sort()) {
    const wrappersForSource = plan.nodes.filter(
      (n) =>
        (n.is_wrapper || n.id.startsWith('MN_WRAP_') || n.id.startsWith('WRAP_') || n.id.startsWith('L1_')) &&
        (n.inputs.includes(source) || n.id === `MN_WRAP_${source}` || n.id === `WRAP_${source}` || n.id === `L1_${source}`),
    );
    if (wrappersForSource.length === 0) {
      errors.push(`Source '${source}' has 0 wrappers; expected exactly one wrapper.`);
    } else if (wrappersForSource.length > 1) {
      errors.push(`Source '${source}' has ${wrappersForSource.length} wrappers; expected exactly one wrapper.`);
    }
  }

  // 5. Wrapper with filter enabled and an empty condition
  for (const node of plan.nodes) {
    const isWrap = node.is_wrapper || node.id.startsWith('MN_WRAP_') || node.id.startsWith('WRAP_') || node.id.startsWith('L1_');
    if (isWrap && node.is_filter_enabled) {
      if (!node.details?.condition || !node.details.condition.trim()) {
        errors.push(`Wrapper '${node.id}' has filter enabled but condition is empty.`);
      }
    }
  }

  // 6. Column existence checks
  // A) Wrapper filter identifier
  for (const node of plan.nodes) {
    const isWrap = node.is_wrapper || node.id.startsWith('MN_WRAP_') || node.id.startsWith('WRAP_') || node.id.startsWith('L1_');
    if (isWrap && node.details?.condition && node.details.condition.trim()) {
      const tokens = node.details.condition.match(/[A-Za-z_][A-Za-z0-9_]*/g) || [];
      const funcMatches = node.details.condition.matchAll(/([A-Za-z_][A-Za-z0-9_]*)\s*\(/g);
      const funcNames = new Set(Array.from(funcMatches, (m) => m[1]));
      const wrapCols = new Set(node.columns);
      for (const token of tokens) {
        if (SQL_KEYWORDS.has(token.toUpperCase()) || funcNames.has(token)) {
          continue;
        }
        if (!wrapCols.has(token)) {
          errors.push(`Wrapper '${node.id}' filter condition references column '${token}' which is missing from wrapper.`);
        }
      }
    }
  }

  // B) Join keys on the fact node
  if (factNode && factNode.details?.on) {
    for (const pair of factNode.details.on) {
      for (const reference of pair) {
        const separator = reference.lastIndexOf('.');
        const col = separator > -1 ? reference.slice(separator + 1).trim() : reference.trim();
        const qualifier = separator > -1 ? reference.slice(0, separator).trim() : null;
        if (qualifier) {
          const inpNode = plan.nodes.find(
            (n) => n.id === qualifier || (n.is_wrapper && n.inputs.includes(qualifier)),
          );
          if (inpNode) {
            if (!inpNode.columns.includes(col)) {
              errors.push(`Fact node '${factNode.id}' join key '${reference}' column '${col}' is missing from input '${inpNode.id}'.`);
            }
          }
        } else {
          const foundInWrap = plan.nodes.some(
            (n) => factNode.inputs.includes(n.id) && n.columns.includes(col),
          );
          if (!foundInWrap) {
            errors.push(`Fact node '${factNode.id}' join key '${reference}' column '${col}' is missing from fact wrapper inputs.`);
          }
        }
      }
    }
  }

  // C) Formula identifiers in calculated fields
  if (factNode) {
    const calcOutputNames = new Set((factNode.calculated_fields || []).map((cf) => cf.name));
    const factCols = new Set([...factNode.columns, ...Array.from(calcOutputNames)]);
    for (const cf of factNode.calculated_fields || []) {
      if (cf.formula) {
        const tokens = cf.formula.match(/[A-Za-z_][A-Za-z0-9_]*/g) || [];
        const funcMatches = cf.formula.matchAll(/([A-Za-z_][A-Za-z0-9_]*)\s*\(/g);
        const funcNames = new Set(Array.from(funcMatches, (m) => m[1]));
        for (const token of tokens) {
          if (SQL_KEYWORDS.has(token.toUpperCase()) || funcNames.has(token)) {
            continue;
          }
          if (!factCols.has(token) && !calcOutputNames.has(token)) {
            const inWrapper = plan.nodes.some(
              (n) => factNode.inputs.includes(n.id) && n.columns.includes(token),
            );
            if (!inWrapper) {
              errors.push(`Fact node '${factNode.id}' calculated field '${cf.name}' formula references column '${token}' which is missing.`);
            }
          }
        }
      }
    }
  }

  // D) Measure column
  if (factNode) {
    const factCols = new Set([
      ...factNode.columns,
      ...(factNode.calculated_fields || []).map((cf) => cf.name),
    ]);
    for (const m of plan.measures || []) {
      if (m.aggregation?.toUpperCase() !== 'FORMULA' && m.column && m.column !== '*' && m.column !== '1') {
        const cleanCol = m.column.split('.').pop()?.trim() || m.column;
        if (!factCols.has(cleanCol)) {
          errors.push(`Measure '${m.name}' references column '${m.column}' which is missing from fact node '${factNode.id}'.`);
        }
      }
    }
  }

  // E) Dimension
  if (factNode) {
    const factCols = new Set([
      ...factNode.columns,
      ...(factNode.calculated_fields || []).map((cf) => cf.name),
    ]);
    for (const dim of plan.dimensions || []) {
      const cleanDim = dim.split('.').pop()?.trim() || dim;
      if (!factCols.has(cleanDim)) {
        errors.push(`Dimension '${dim}' is missing from fact node '${factNode.id}'.`);
      }
    }
  }

  return {
    errors,
    warnings,
  };
}
