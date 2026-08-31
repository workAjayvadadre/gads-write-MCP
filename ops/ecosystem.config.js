// PM2 process definition for gads-write-mcp.
//
//   pm2 start ops/ecosystem.config.js
//   pm2 logs gads-write-mcp
//   pm2 restart gads-write-mcp      # picks up config/*.yaml and .env changes
//   pm2 save                        # persist across reboot
//
// This is a SEPARATE process from the read-only google-ads-mcp. Different
// name, different port, different repo. Do not merge them.

module.exports = {
  apps: [
    {
      name: "gads-write-mcp",
      script: "./ops/run.sh",
      interpreter: "bash",
      cwd: "/home/ubuntu/gads-write-mcp", // REPLACE with the real deploy path

      instances: 1,
      // Must stay 1. Draft plans (Phase 2) are held in process memory, so a
      // second worker would not see a plan drafted by the first, and
      // single-use enforcement would have a hole in it.
      exec_mode: "fork",

      autorestart: true,
      max_restarts: 10,
      min_uptime: "20s", // a boot-time ConfigError exits fast; do not loop on it
      restart_delay: 4000,
      max_memory_restart: "500M",

      // The app reads .env itself (python-dotenv). Anything here is a
      // fallback only. Never put secrets in this file — it is committed.
      env: {
        PYTHONUNBUFFERED: "1", // stream logs to PM2 immediately
      },

      out_file: "./logs/pm2-out.log",
      error_file: "./logs/pm2-error.log",
      merge_logs: true,
      time: true,
    },
  ],
};
