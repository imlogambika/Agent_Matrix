// The audit is only as good as its evidence: agents may read dataset/, never change it.
import { customPolicies, allow, deny } from "failproofai";

const EVIDENCE = /(^|[\s"'=\/])dataset\//;
const MUTATING = [
  /\b(rm|rmdir|unlink|shred|truncate|mv|chmod|chown|touch)\b/,
  /\bsed\b[^|;&]*\s-i/,
  />>?\s*\S*dataset\//,
  /\btee\b[^|;&]*dataset\//,
  /\b(cp|install|rsync)\b[^|;&]*\sdataset\/\S*\s*$/,
  /\bgit\s+(checkout|restore|clean|rm|reset|mv)\b[^|;&]*dataset/,
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
        return deny(`${path} is audit evidence and read-only. Write results under code/ instead.`);
      }
      return allow();
    }
    if (tool !== "Bash") return allow();
    const command = String(ctx.toolInput?.command ?? "");
    if (EVIDENCE.test(command) && MUTATING.some((re) => re.test(command))) {
      return deny("This command would change audit evidence under dataset/, which is read-only. Copy the file " +
                  "to a scratch directory if a test needs a modified version.");
    }
    return allow();
  },
});
