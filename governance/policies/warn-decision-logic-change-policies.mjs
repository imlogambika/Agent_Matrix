// The completion guard, the deletion policy and state reconstruction decide whether a deletion counts as
// complete. Changing them is sometimes right, so this is oversight, not a block: the agent must stop,
// tell the user, and prove nothing regressed.
import { customPolicies, allow, instruct } from "failproofai";

const DECISION_LOGIC = /code\/ghost_audit\/(guard|policy|state|review)\.py$/;

customPolicies.add({
  name: "warn-decision-logic-change",
  description: "Oversight before an agent edits the code that decides whether a deletion is complete",
  match: { events: ["PreToolUse"] },
  fn: async (ctx) => {
    if (!["Write", "Edit", "MultiEdit"].includes(ctx.toolName ?? "")) return allow();
    const path = String(ctx.toolInput?.file_path ?? "");
    if (!DECISION_LOGIC.test(path)) return allow();
    return instruct(`STOP: ${path} decides whether a deletion is reported COMPLETE. Confirm the change with the ` +
                    "user before executing, then run python3 code/demo.py safety and python3 -m unittest " +
                    "discover -s code/tests and report the results.");
  },
});
