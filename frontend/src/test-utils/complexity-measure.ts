/**
 * Complexity measurement for the frontend complexity-budget ratchet.
 *
 * The backend has complexity gates; the frontend had none, and the result is
 * visible in the size distribution: `views/PipelineEditorView.vue` is 6,452
 * lines and `views/RunDetailView.vue` 2,659. This module is the measuring
 * instrument `__tests__/complexity-budget.spec.ts` enforces; it is deliberately
 * dependency-free beyond what the repo already ships (`@vue/compiler-sfc` and
 * `typescript`), and never shells out to eslint.
 *
 * What is measured, per file
 * -------------------------
 * 1. `lines` - every physical line in the file, template and `<style>` included,
 *    so a 6k-line single-file component is caught even when its *functions* are
 *    small. Definition: the number of `\n` characters, plus one when the file
 *    does not end with a newline, and 0 for an empty file. This is stable across
 *    CRLF/LF because only `\n` is counted.
 * 2. `maxFunctionComplexity` - the classic McCabe cyclomatic complexity of the
 *    most complex function/method in the file: `1 + decision points`. A file
 *    with no functions reports 0 (there is nothing to be complex).
 *
 * Decision points counted (one each)
 * ----------------------------------
 * - `if` (including every `else if`)
 * - `for`, `for...in`, `for...of`, `while`, `do...while`
 * - `case` of every `switch` with a test; `default` is not a branch
 * - `catch`
 * - ternary conditional expressions (`a ? b : c`)
 * - `&&`, `||`, `??`
 * - `&&=`, `||=`, `??=` (short-circuiting assignments)
 * - optional chaining: `a?.b`, `a?.[b]`, `a?.(b)` - one point per `?.`
 *
 * Scope of the walk
 * -----------------
 * - `.vue` files: only the `<script>` / `<script setup>` blocks are walked,
 *   located via `@vue/compiler-sfc`'s `parse()`. The `<template>` is not walked.
 *   Template expressions are a different language (the Vue template compiler's
 *   own AST) and their complexity is overwhelmingly driven by the script that
 *   feeds them; a template-only balloon is still caught by `lines`. This is a
 *   known, documented limitation, not an oversight.
 * - Nested functions are measured separately and their bodies are NOT charged to
 *   the enclosing function (the standard convention). Every nested callback is
 *   still measured in its own right, so the file maximum cannot be hidden in a
 *   closure.
 * - Module-level (top-level) decision points belong to no function and are not
 *   counted. In a `<script setup>` block that is the setup body itself; the
 *   `lines` ceiling is what bounds it.
 */
import { parse as parseSfc } from '@vue/compiler-sfc'
import * as ts from 'typescript'

/** One function/method measured inside a script block. */
export interface FunctionComplexity {
  /** Best-effort name (`const foo = () => {}` reports `foo`); `<anonymous>` otherwise. */
  name: string
  /** 1-based line in the ORIGINAL file (a `.vue` block's offset is applied). */
  line: number
  /** Number of decision points found in the function body (nested functions excluded). */
  decisions: number
  /** `1 + decisions` (McCabe). */
  complexity: number
}

/** The two numbers the budget pins, plus detail for failure messages. */
export interface FileMeasurement {
  lines: number
  /** 0 when the file defines no function. */
  maxFunctionComplexity: number
  /** The function that set `maxFunctionComplexity`, or null when there is none. */
  worstFunction: FunctionComplexity | null
  functionCount: number
}

/** A script block extracted from a file, with the offset needed to report real lines. */
export interface ScriptBlock {
  /** The block's source text. */
  content: string
  /** Line offset to add to a 1-based line inside `content` to get the file line. */
  lineOffset: number
}

const DECISION_BINARY_OPERATORS = new Set<ts.SyntaxKind>([
  ts.SyntaxKind.AmpersandAmpersandToken,
  ts.SyntaxKind.BarBarToken,
  ts.SyntaxKind.QuestionQuestionToken,
  ts.SyntaxKind.AmpersandAmpersandEqualsToken,
  ts.SyntaxKind.BarBarEqualsToken,
  ts.SyntaxKind.QuestionQuestionEqualsToken,
])

const DECISION_STATEMENTS = new Set<ts.SyntaxKind>([
  ts.SyntaxKind.IfStatement,
  ts.SyntaxKind.ForStatement,
  ts.SyntaxKind.ForInStatement,
  ts.SyntaxKind.ForOfStatement,
  ts.SyntaxKind.WhileStatement,
  ts.SyntaxKind.DoStatement,
  ts.SyntaxKind.CatchClause,
])

/** Physical line count: `\n` count, plus one for a final unterminated line. */
export function countLines(source: string): number {
  if (source.length === 0) return 0
  let newlines = 0
  for (let index = 0; index < source.length; index += 1) {
    if (source.charCodeAt(index) === 10) newlines += 1
  }
  return source.endsWith('\n') ? newlines : newlines + 1
}

/**
 * The script blocks of a file. A `.vue` file is parsed with
 * `@vue/compiler-sfc`; a `.ts` file is its own only block.
 *
 * A structurally invalid SFC throws, naming the file, because silently
 * measuring zero would let a broken file slip through the gate.
 */
export function extractScriptBlocks(source: string, filename: string): ScriptBlock[] {
  if (!filename.endsWith('.vue')) {
    return [{ content: source, lineOffset: 0 }]
  }
  const { descriptor, errors } = parseSfc(source, { filename })
  if (errors.length > 0) {
    const detail = errors.map((error) => (error instanceof Error ? error.message : String(error))).join('; ')
    throw new Error(`cannot parse ${filename}: ${detail}`)
  }
  const blocks: ScriptBlock[] = []
  for (const block of [descriptor.script, descriptor.scriptSetup]) {
    if (!block) continue
    blocks.push({ content: block.content, lineOffset: block.loc.start.line - 1 })
  }
  return blocks
}

function hasBody(node: ts.Node): node is ts.FunctionLikeDeclaration & { body: ts.Node } {
  switch (node.kind) {
    case ts.SyntaxKind.FunctionDeclaration:
    case ts.SyntaxKind.FunctionExpression:
    case ts.SyntaxKind.ArrowFunction:
    case ts.SyntaxKind.MethodDeclaration:
    case ts.SyntaxKind.Constructor:
    case ts.SyntaxKind.GetAccessor:
    case ts.SyntaxKind.SetAccessor:
      return (node as ts.FunctionLikeDeclaration).body !== undefined
    default:
      return false
  }
}

function describeFunction(node: ts.Node, sourceFile: ts.SourceFile): string {
  const named = node as ts.NamedDeclaration
  if (named.name) {
    if (ts.isIdentifier(named.name)) return named.name.text
    // `{ handler() {} }`, `class { handle = () => {} }`, `'quoted-key'() {}`.
    const text = named.name.getText(sourceFile)
    if (text.length > 0) return text
  }
  const parent = node.parent
  if (parent && ts.isVariableDeclaration(parent) && ts.isIdentifier(parent.name)) {
    return parent.name.text
  }
  if (parent && ts.isPropertyAssignment(parent)) {
    return parent.name.getText(sourceFile)
  }
  return '<anonymous>'
}

function decisionWeight(node: ts.Node): number {
  if (DECISION_STATEMENTS.has(node.kind)) return 1
  switch (node.kind) {
    case ts.SyntaxKind.ConditionalExpression:
      return 1
    case ts.SyntaxKind.CaseClause:
      // `default:` carries no expression and is not a branch.
      return (node as ts.CaseClause).expression ? 1 : 0
    case ts.SyntaxKind.BinaryExpression:
      return DECISION_BINARY_OPERATORS.has((node as ts.BinaryExpression).operatorToken.kind) ? 1 : 0
    case ts.SyntaxKind.PropertyAccessExpression:
      return (node as ts.PropertyAccessExpression).questionDotToken ? 1 : 0
    case ts.SyntaxKind.ElementAccessExpression:
      return (node as ts.ElementAccessExpression).questionDotToken ? 1 : 0
    case ts.SyntaxKind.CallExpression:
      return (node as ts.CallExpression).questionDotToken ? 1 : 0
    default:
      return 0
  }
}

interface WalkContext {
  sourceFile: ts.SourceFile
  lineOffset: number
  functions: FunctionComplexity[]
}

function visit(node: ts.Node, counter: { decisions: number }, context: WalkContext): void {
  if (hasBody(node)) {
    const inner = { decisions: 0 }
    const name = describeFunction(node, context.sourceFile)
    const start = node.getStart(context.sourceFile)
    const line = context.sourceFile.getLineAndCharacterOfPosition(start).line + 1 + context.lineOffset
    // The function's own parameters, decorators and body are walked into the
    // fresh counter: a nested function cannot charge its branches to its host.
    ts.forEachChild(node, (child) => visit(child, inner, context))
    context.functions.push({ name, line, decisions: inner.decisions, complexity: 1 + inner.decisions })
    return
  }
  counter.decisions += decisionWeight(node)
  ts.forEachChild(node, (child) => visit(child, counter, context))
}

/** Measure one script block. Exported for the spec's positive controls. */
export function measureScript(block: ScriptBlock, filename: string): FunctionComplexity[] {
  const scriptKind = filename.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS
  const sourceFile = ts.createSourceFile(
    filename,
    block.content,
    ts.ScriptTarget.Latest,
    /* setParentNodes */ true,
    scriptKind,
  )
  const context: WalkContext = { sourceFile, lineOffset: block.lineOffset, functions: [] }
  // Top-level statements have no host function; their decision points are
  // intentionally not attributed (see the module docstring).
  visit(sourceFile, { decisions: 0 }, context)
  return context.functions
}

/** Measure a whole file's source. Exported so the spec can prove the gate fires. */
export function measureSource(source: string, filename: string): FileMeasurement {
  const blocks = extractScriptBlocks(source, filename)
  const functions = blocks.flatMap((block) => measureScript(block, filename))
  let worst: FunctionComplexity | null = null
  for (const fn of functions) {
    if (!worst || fn.complexity > worst.complexity) worst = fn
  }
  return {
    lines: countLines(source),
    maxFunctionComplexity: worst ? worst.complexity : 0,
    worstFunction: worst,
    functionCount: functions.length,
  }
}
