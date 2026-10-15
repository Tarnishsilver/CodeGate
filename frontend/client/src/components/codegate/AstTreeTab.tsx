/**
 * AST tab — renders the real Abstract Syntax Tree produced by the backend
 * with the same interactive node-graph canvas as the /graph page, so every
 * analyzed snippet gets the full graph treatment (drag, search, depth
 * control, node inspector, fullscreen).
 */
import AstGraphVisualizer from "@/components/AstGraphVisualizer";
import type { AstNode } from "@/lib/codegate";

export default function AstTreeTab({ ast, filename }: { ast: AstNode | null; filename: string }) {
  if (!ast) {
    return <p className="text-xs text-zinc-500 p-6 font-sans">Run an analysis to see the AST.</p>;
  }

  return <AstGraphVisualizer tree={ast} sourceLabel={filename} sourceKind="editor" minimal />;
}
