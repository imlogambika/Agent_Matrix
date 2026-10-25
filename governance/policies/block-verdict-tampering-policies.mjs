// A deletion verdict may only come from the guarded pipeline (python3 code/main.py), whose completion
// guard refuses to write output.csv unless every COMPLETE is backed by admissible evidence. An agent that
// edits output.csv or the review queue by hand bypasses that guard, so it is blocked.
import { customPolicies, allow, deny } from "failproofai";

// The commands a shell line actually runs: heredoc bodies are data, not commands.
function commands(line) {
  const code = line.replace(/<<-?\s*['"]?(\w+)['"]?([^\n]*)\n[\s\S]*?\n\s*\1\s*(?=\n|$)/g, "<<$1$2");
  return code.split(/\n|;|&&|\|\||\|/).map((s) => s.trim().replace(/^((sudo|env)\s+|\w+=\S*\s+)*/, "")).filter(Boolean);
}

const VERDICTS = /(^|[\s"'=\/])(output(\.blocked)?\.csv|(sample_|subset_)?review_queue\.csv|escalation_report\.md)\b/;
const T = "(output(\\.blocked)?\\.csv|review_queue\\.csv|escalation_report\\.md)";
const writesVerdict = (cmd) =>
  new RegExp(`>>?\\s*\\S*${T}`).test(cmd) ||                       // redirect onto a verdict file
  new RegExp(`^(sed|perl)\\b.*\\s-[a-z]*i.*${T}`).test(cmd) ||       // in-place edit
  new RegExp(`^tee\\b.*${T}`).test(cmd) ||
  new RegExp(`^(cp|install)\\b.*\\s\\S*${T}\\s*$`).test(cmd) ||      // copy over it
  new RegExp(`^(mv|rm|truncate)\\b.*${T}`).test(cmd);                 // replace or delete it

customPolicies.add({
  name: "block-verdict-tampering",
  description: "output.csv and the review queue are written only by the guarded audit pipeline, never by hand",
  match: { events: ["PreToolUse"] },
  fn: async (ctx) => {
    const tool = ctx.toolName ?? "";
    if (["Write", "Edit", "MultiEdit"].includes(tool)) {
      const path = String(ctx.toolInput?.file_path ?? "");
      if (VERDICTS.test(path)) {
        return deny(`${path} holds audit verdicts. Change the pipeline and re-run python3 code/main.py; ` +
                    "its completion guard decides what may be written.");
      }
      return allow();
    }
    if (tool !== "Bash") return allow();
    if (commands(String(ctx.toolInput?.command ?? "")).some((c) => VERDICTS.test(c) && writesVerdict(c))) {
      return deny("Writing audit verdicts by hand bypasses the completion guard. Re-run python3 code/main.py instead.");
    }
    return allow();
  },
});
