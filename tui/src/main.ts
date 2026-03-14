#!/usr/bin/env node

import { NanobotClient } from "./api/client.js";
import { App } from "./ui/app.js";

function parseArgs(): { host: string; port: number } {
  const args = process.argv.slice(2);
  let host = "127.0.0.1";
  let port = 18790;

  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--host" && args[i + 1]) {
      host = args[++i];
    } else if (args[i] === "--port" && args[i + 1]) {
      port = parseInt(args[++i], 10);
    }
  }

  return { host, port };
}

async function main(): Promise<void> {
  const { host, port } = parseArgs();
  const client = new NanobotClient(host, port);

  try {
    await client.healthCheck();
  } catch {
    console.error(
      `Cannot connect to nanobot gateway at ${host}:${port}`
    );
    console.error("Start it with: nanobot gateway --port " + port);
    process.exit(1);
  }

  const app = new App(client);
  await app.start();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
