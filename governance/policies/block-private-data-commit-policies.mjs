// .gitignore keeps the dataset, model weights and secrets out of the repository; agents may not route around it.
import { customPolicies, allow, deny } from "failproofai";

customPolicies.add({
  name: "block-private-data-commit",
  description: "Never stage the dataset, model weights or credentials, and never force-add ignored files",
  match: { events: ["PreToolUse"] },
  fn: async (ctx) => {
    if (ctx.toolName !== "Bash") return allow();
    const command = String(ctx.toolInput?.command ?? "");
    if (!/\bgit\s+add\b/.test(command)) return allow();
    if (/\bgit\s+add\b[^|;&]*(\s-f\b|\s--force\b)/.test(command)) {
      return deny("git add --force bypasses .gitignore, which keeps the dataset, weights and secrets out of git. " +
                  "Stage the specific source files instead.");
    }
    if (/\bgit\s+add\b[^|;&]*(dataset|\.env\b|\.gguf\b|credentials|\.pem\b|\.venv)/.test(command)) {
      return deny("That path holds evidence, model weights or credentials and must not be committed. " +
                  "Stage source, tests and docs only.");
    }
    return allow();
  },
});
