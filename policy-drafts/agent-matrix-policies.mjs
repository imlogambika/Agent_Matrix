// Agent Matrix project policies for the coding agents that work on this repository.
//
// The agents audit evidence; they must never be able to change it, and the evidence must
// never leave the machine through git. No builtin protects a data directory, so these
// two rules are custom. Installed by the operator (the agent itself is not allowed to);
// see the README section "Failproof AI".
import { customPolicies, allow, deny } from "failproofai";

const EVIDENCE = /(^|[\s"'=\/])dataset\//;
const MUTATING = [
  /\b(rm|rmdir|unlink|shred|truncate|mv|chmod|chown)\b/,   // delete, move, alter
  /\bsed\b[^|;&]*\s-i/,                                     // in-place edit
  />>?\s*\S*dataset\//,                                     // redirect onto evidence
  /\btee\b[^|;&]*dataset\//,
  /\bcp\b[^|;&]*\sdataset\/\S*\s*$/,                        // copy over evidence
  /\bgit\s+(checkout|restore|clean|rm|reset)\b[^|;&]*dataset/,
];

customPolicies.add({
  name: "protect-audit-evidence",
  description: "Evidence under dataset/ is read-only for agents: no delete, move, overwrite or in-place edit",
  match: { events: ["PreToolUse"] },
  fn: async (ctx) => {
    const tool = ctx.toolName ?? "";
    if (["Write", "Edit", "MultiEdit", "NotebookEdit"].includes(tool)) {
      const path = String(ctx.toolInput?.file_path ?? "");
      if (/(^|\/)dataset\//.test(path)) {
        return deny(`${path} is audit evidence and is read-only. Write results under code/ or output.csv instead.`);
      }
      return allow();
    }
    if (tool !== "Bash") return allow();
    const command = String(ctx.toolInput?.command ?? "");
    if (!EVIDENCE.test(command)) return allow();
    if (MUTATING.some((re) => re.test(command))) {
      return deny("This command would change audit evidence under dataset/. Evidence is read-only: copy it to a " +
                  "scratch directory if you need a modified version for a test.");
    }
    return allow();
  },
});

customPolicies.add({
  name: "block-private-data-commit",
  description: "Never stage the dataset, model weights or credentials",
  match: { events: ["PreToolUse"] },
  fn: async (ctx) => {
    if (ctx.toolName !== "Bash") return allow();
    const command = String(ctx.toolInput?.command ?? "");
    if (!/\bgit\s+add\b/.test(command)) return allow();
    if (/\bgit\s+add\b[^|;&]*(\s-f\b|\s--force\b)/.test(command)) {
      return deny("git add --force bypasses .gitignore, which keeps the dataset, weights and secrets out of git. " +
                  "Stage the specific source files instead.");
    }
    if (/\bgit\s+add\b[^|;&]*(dataset|\.env\b|\.gguf\b|credentials|\.pem\b)/.test(command)) {
      return deny("That path holds evidence, model weights or credentials and must not be committed. " +
                  "Stage source, tests and docs only.");
    }
    return allow();
  },
});
