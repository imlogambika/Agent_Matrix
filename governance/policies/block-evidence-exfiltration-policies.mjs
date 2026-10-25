// Privacy evidence (requests, identities, logs, screenshots) never leaves the machine through an agent.
import { customPolicies, allow, deny } from "failproofai";

// Heredoc bodies are data, not commands; the rest is split into the commands the line runs.
const stripHeredocs = (line) => line.replace(/<<-?\s*['"]?(\w+)['"]?([^\n]*)\n[\s\S]*?\n\s*\1\s*(?=\n|$)/g, "<<$1$2");
const commands = (code) =>
  code.split(/\n|;|&&|\|\||\|/).map((s) => s.trim().replace(/^((sudo|env)\s+|\w+=\S*\s+)*/, "")).filter(Boolean);

const PRIVATE = /(dataset\/|user_identities\.csv|user_audit_log\.csv|deletion_requests\.csv|llm_cache\.json)/;
const uploads = (cmd) =>
  /^curl\b.*(\s-(F|T|d)\b|--data|--form|--upload-file|-X\s*(POST|PUT))/.test(cmd) ||
  /^wget\b.*--post-(file|data)/.test(cmd) ||
  /^(scp|sftp|ftp)\b/.test(cmd) ||
  /^rsync\b.*\S+:/.test(cmd) ||
  /^(nc|ncat|netcat|socat)\b/.test(cmd) ||
  /^gh\s+(gist\s+create|release\s+upload)\b/.test(cmd) ||
  /^(aws\s+s3\s+(cp|sync)|gsutil\s+cp|rclone\s+(copy|sync))\b/.test(cmd);

customPolicies.add({
  name: "block-evidence-exfiltration",
  description: "Never upload audit evidence, identities or model replies to a remote host",
  match: { events: ["PreToolUse"] },
  fn: async (ctx) => {
    if (ctx.toolName !== "Bash") return allow();
    const code = stripHeredocs(String(ctx.toolInput?.command ?? ""));
    // a pipe carries data between commands, so the private file and the upload may sit in different ones
    if (PRIVATE.test(code) && commands(code).some(uploads)) {
      return deny("This would send privacy evidence off the machine. Share aggregate results (output.csv, " +
                  "reports) instead, and only where the user asked.");
    }
    return allow();
  },
});
