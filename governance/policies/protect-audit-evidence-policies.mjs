// The audit is only as good as its evidence: agents may read dataset/, never change it.
import { customPolicies, allow, deny } from "failproofai";

// The commands a shell line actually runs: heredoc bodies are data, not commands.
function commands(line) {
  const code = line.replace(/<<-?\s*['"]?(\w+)['"]?([^\n]*)\n[\s\S]*?\n\s*\1\s*(?=\n|$)/g, "<<$1$2");
  return code.split(/\n|;|&&|\|\||\|/).map((s) => s.trim().replace(/^((sudo|env)\s+|\w+=\S*\s+)*/, "")).filter(Boolean);
}

const EVIDENCE = /(^|[\s"'=\/])dataset\//;
const changesEvidence = (cmd) =>
  /^(rm|rmdir|unlink|shred|truncate|mv|chmod|chown|touch)\b/.test(cmd) ||    // delete, move, alter, plant
  /^(sed|perl)\b.*\s-[a-z]*i/.test(cmd) ||                                   // in-place edit
  />>?\s*\S*dataset\//.test(cmd) ||                                          // redirect onto evidence
  /^tee\b.*dataset\//.test(cmd) ||
  /^(cp|install|rsync)\b.*\sdataset\/\S*\s*$/.test(cmd) ||                   // copy over evidence
  /^git\s+(-C\s+\S+\s+)?(checkout|restore|clean|rm|reset|mv)\b.*dataset/.test(cmd);

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
    if (commands(String(ctx.toolInput?.command ?? "")).some((c) => EVIDENCE.test(c) && changesEvidence(c))) {
      return deny("This command would change audit evidence under dataset/, which is read-only. Copy the file " +
                  "to a scratch directory if a test needs a modified version.");
    }
    return allow();
  },
});
