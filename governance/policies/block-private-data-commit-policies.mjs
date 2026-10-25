// .gitignore keeps the dataset, model weights and secrets out of the repository; agents may not route around it.
import { customPolicies, allow, deny } from "failproofai";

// The commands a shell line actually runs: heredoc bodies are data, not commands (v2: prose such as a log
// entry saying "git add of dataset/" inside a heredoc was blocked in enforce mode).
function commands(line) {
  const code = line.replace(/<<-?\s*['"]?(\w+)['"]?([^\n]*)\n[\s\S]*?\n\s*\1\s*(?=\n|$)/g, "<<$1$2");
  return code.split(/\n|;|&&|\|\||\|/).map((s) => s.trim().replace(/^((sudo|env)\s+|\w+=\S*\s+)*/, "")).filter(Boolean);
}

customPolicies.add({
  name: "block-private-data-commit",
  description: "Never stage the dataset, model weights or credentials, and never force-add ignored files",
  match: { events: ["PreToolUse"] },
  fn: async (ctx) => {
    if (ctx.toolName !== "Bash") return allow();
    for (const cmd of commands(String(ctx.toolInput?.command ?? ""))) {
      if (!/^git\s+(-C\s+\S+\s+)?add\b/.test(cmd)) continue;
      if (/\s(-f|--force)\b/.test(cmd)) {
        return deny("git add --force bypasses .gitignore, which keeps the dataset, weights and secrets out of git. " +
                    "Stage the specific source files instead.");
      }
      if (/(dataset|\.env\b|\.gguf\b|credentials|\.pem\b|\.venv)/.test(cmd)) {
        return deny("That path holds evidence, model weights or credentials and must not be committed. " +
                    "Stage source, tests and docs only.");
      }
    }
    return allow();
  },
});
