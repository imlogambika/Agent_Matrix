// A deletion verdict may only come from the guarded pipeline (python3 code/main.py), whose completion
// guard refuses to write output.csv unless every COMPLETE is backed by admissible evidence. An agent that
// edits output.csv or the review queue by hand bypasses that guard, so it is blocked.
import { customPolicies, allow, deny } from "failproofai";

const VERDICTS = /(^|[\s"'=\/])(output(\.blocked)?\.csv|(sample_|subset_)?review_queue\.csv|escalation_report\.md)\b/;
const TARGET = "(output(\\.blocked)?\\.csv|review_queue\\.csv|escalation_report\\.md)";
const WRITES = [
  new RegExp(`>>?\\s*\\S*${TARGET}`),                            // redirect onto a verdict file
  new RegExp(`\\b(sed|perl)\\b[^|;&]*\\s-[a-z]*i[^|;&]*${TARGET}`),     // in-place edit
  new RegExp(`\\btee\\b[^|;&]*${TARGET}`),
  new RegExp(`\\b(cp|install)\\b[^|;&]*\\s\\S*${TARGET}\\s*$`),       // copy over it
  new RegExp(`\\b(mv|rm|truncate)\\b[^|;&]*${TARGET}`),             // replace or delete it
];

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
    const command = String(ctx.toolInput?.command ?? "");
    if (VERDICTS.test(command) && WRITES.some((re) => re.test(command))) {
      return deny("Writing audit verdicts by hand bypasses the completion guard. Re-run python3 code/main.py instead.");
    }
    return allow();
  },
});
