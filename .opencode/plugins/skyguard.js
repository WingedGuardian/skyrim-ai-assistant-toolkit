// Generated from agent/targets/opencode/plugins/skyguard.js. OpenCode V1 only.
// One export: OpenCode invokes every exported plugin factory.
import { fileURLToPath } from "node:url"
import { createGuard } from "../../tools/modding_harness/opencode_plugin.mjs"

export const SkyrimGuard = createGuard({
  label: "Skyrim",
  envPrefix: "SKYRIM",
  verdictPrefix: "SKYGUARD-OPENCODE",
  // Mirror mcp_rules.judged_server: a DevBench server (any spelling, including names containing double
  // underscores), or a named write/console tool on any server (rules.MCP_WRITE_TOOLS, MCP_CONSOLE_TOOLS).
  // Other MCP tools keep the shared transport's unknown-tool receipt and warning behavior.
  isJudgedTool: tool => {
    const parts = typeof tool === "string" ? tool.split("__") : []
    return parts.length >= 3 && parts[0] === "mcp" &&
      (parts.slice(1).join("__").toLowerCase().replace(/[-_.\s]/g, "").includes("devbench") ||
       ["installplugin", "sortloadorder", "cleanplugins", "executeconsolecommand", "callpapyrusfunction"]
         .includes(parts[parts.length - 1].toLowerCase().replace(/[-_.\s]/g, "")))
  },
  adapter: fileURLToPath(new URL("../hooks/skyguard_adapter.py", import.meta.url)),
})
