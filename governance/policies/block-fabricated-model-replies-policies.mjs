// code/cache/llm_cache.json holds what the open models actually answered. The offline run and the usage
// report replay it, so a hand edit would fabricate a model's answer (for example turning a request into a
// deletion). Only the model client may write it.
import { customPolicies, allow, deny } from "failproofai";

// The commands a shell line actually runs: heredoc bodies are data, not commands.
function commands(line) {
  const code = line.replace(/<<-?\s*['"]?(\w+)['"]?([^\n]*)\n[\s\S]*?\n\s*\1\s*(?=\n|$)/g, "<<$1$2");
  return code.split(/\n|;|&&|\|\||\|/).map((s) => s.trim().replace(/^((sudo|env)\s+|\w+=\S*\s+)*/, "")).filter(Boolean);
}

const CACHE = /llm_cache\.json/;
const writesCache = (cmd) =>
  />>?\s*\S*llm_cache\.json/.test(cmd) ||
  /^(sed|perl)\b.*\s-[a-z]*i.*llm_cache/.test(cmd) ||
  /^tee\b.*llm_cache/.test(cmd) ||
  /^(cp|install)\b.*\s\S*llm_cache\.json\s*$/.test(cmd) ||
  /^(mv|rm|truncate)\b.*llm_cache/.test(cmd);

customPolicies.add({
  name: "block-fabricated-model-replies",
  description: "The model reply cache is written only by the model client, never edited by an agent",
  match: { events: ["PreToolUse"] },
  fn: async (ctx) => {
    const tool = ctx.toolName ?? "";
    if (["Write", "Edit", "MultiEdit"].includes(tool)) {
      const path = String(ctx.toolInput?.file_path ?? "");
      return CACHE.test(path)
        ? deny("llm_cache.json records real model answers; editing it fabricates them. Re-run the models instead.")
        : allow();
    }
    if (tool !== "Bash") return allow();
    if (commands(String(ctx.toolInput?.command ?? "")).some((c) => CACHE.test(c) && writesCache(c))) {
      return deny("llm_cache.json records real model answers and is written only by the model client. " +
                  "Use python3 code/main.py --no-cache to regenerate replies.");
    }
    return allow();
  },
});
