// Privacy evidence (requests, identities, logs, screenshots) never leaves the machine through an agent.
import { customPolicies, allow, deny } from "failproofai";

const PRIVATE = /(dataset\/|user_identities\.csv|user_audit_log\.csv|deletion_requests\.csv|llm_cache\.json)/;
const UPLOAD = [
  /\bcurl\b[^|;&]*(\s-(F|T|d)\b|--data|--form|--upload-file|-X\s*(POST|PUT))/,
  /\bwget\b[^|;&]*--post-(file|data)/,
  /\b(scp|sftp|ftp)\b/,
  /\brsync\b[^|;&]*\S+:/,
  /\b(nc|ncat|netcat|socat)\b/,
  /\bgh\s+(gist\s+create|release\s+upload)\b/,
  /\b(aws\s+s3\s+(cp|sync)|gsutil\s+cp|rclone\s+(copy|sync))\b/,
];

customPolicies.add({
  name: "block-evidence-exfiltration",
  description: "Never upload audit evidence, identities or model replies to a remote host",
  match: { events: ["PreToolUse"] },
  fn: async (ctx) => {
    if (ctx.toolName !== "Bash") return allow();
    const command = String(ctx.toolInput?.command ?? "");
    if (PRIVATE.test(command) && UPLOAD.some((re) => re.test(command))) {
      return deny("This would send privacy evidence off the machine. Share aggregate results (output.csv, " +
                  "reports) instead, and only where the user asked.");
    }
    return allow();
  },
});
