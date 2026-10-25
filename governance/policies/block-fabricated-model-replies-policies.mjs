// code/cache/llm_cache.json holds what the open models actually answered. The offline run and the usage
// report replay it, so a hand edit would fabricate a model's answer (for example turning a request into a
// deletion). Only the model client may write it.
import { customPolicies, allow, deny } from "failproofai";

const CACHE = /llm_cache\.json/;

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
    const command = String(ctx.toolInput?.command ?? "");
    const writes = [/>>?\s*\S*llm_cache\.json/, /\b(sed|perl)\b[^|;&]*\s-[a-z]*i[^|;&]*llm_cache/,
                    /\btee\b[^|;&]*llm_cache/, /\b(cp|install)\b[^|;&]*\s\S*llm_cache\.json\s*$/,
                    /\b(mv|rm|truncate)\b[^|;&]*llm_cache/];
    if (CACHE.test(command) && writes.some((re) => re.test(command))) {
      return deny("llm_cache.json records real model answers and is written only by the model client. " +
                  "Use python3 code/main.py --no-cache to regenerate replies.");
    }
    return allow();
  },
});
