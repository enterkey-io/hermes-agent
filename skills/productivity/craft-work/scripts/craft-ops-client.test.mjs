import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { DEFAULT_TIMEOUT_MS, MAX_MESSAGE_BYTES, callBroker, validateRequest } from './craft-ops-client.mjs';

const SCRIPT_DIR = path.dirname(new URL(import.meta.url).pathname);

test('exports a 15-minute default broker timeout', () => {
  assert.equal(DEFAULT_TIMEOUT_MS, 900_000);
});

async function withServer(handler, run) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'craft-ops-client-'));
  const socketPath = path.join(dir, 'broker.sock');
  const connections = new Set();
  const server = net.createServer({ allowHalfOpen: true }, handler);
  server.on('connection', (connection) => {
    connections.add(connection);
    connection.once('close', () => connections.delete(connection));
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(socketPath, resolve);
  });
  try {
    await run(socketPath);
  } finally {
    for (const connection of connections) connection.destroy();
    await new Promise((resolve) => server.close(resolve));
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

test('sends one strict newline-delimited request to a Unix socket and returns one broker response', async () => {
  await withServer((connection) => {
    let received = '';
    connection.setEncoding('utf8');
    connection.on('data', (chunk) => {
      received += chunk;
      if (!received.endsWith('\n')) return;
      assert.deepEqual(JSON.parse(received), {
        version: '1',
        requestId: '7be5d5a3-bfbe-45eb-9d41-718fa5ea2bfb',
        action: 'contracts',
        input: {},
        dryRun: false,
      });
      connection.end('{"ok":true,"contracts":[]}\n');
    });
  }, async (socketPath) => {
    const result = await callBroker(
      {
        version: '1',
        requestId: '7be5d5a3-bfbe-45eb-9d41-718fa5ea2bfb',
        action: 'contracts',
        input: {},
        dryRun: false,
      },
      { socketPath, timeoutMs: 1_000 },
    );
    assert.deepEqual(result, { ok: true, contracts: [] });
  });
});

async function runClient(args) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, ['./craft-ops-client.mjs', ...args], { cwd: SCRIPT_DIR });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => (stdout += chunk));
    child.stderr.on('data', (chunk) => (stderr += chunk));
    child.once('error', reject);
    child.once('close', (code) => resolve({ code, stdout, stderr }));
  });
}

test('rejects an arbitrary socket path for contracts', async () => {
  let connected = false;
  await withServer((connection) => {
    connected = true;
    connection.end('{"ok":true,"contracts":[]}\n');
  }, async (socketPath) => {
    const result = await runClient(['contracts', '--socket', socketPath]);
    assert.equal(result.code, 2);
    assert.match(result.stdout, /"code":"usage"/);
    assert.equal(result.stderr, 'craft-ops-client: usage\n');
    assert.equal(connected, false);
  });
});

for (const [action, requiredArgs] of [
  ['run', ['--contract', 'meeting-ingest']],
  ['resume', ['--run-id', 'run-1']],
  ['reconcile', ['--run-id', 'run-1']],
  ['rollback', ['--run-id', 'run-1']],
]) {
  test(`rejects --socket for ${action}`, async () => {
    let connected = false;
    await withServer((connection) => {
      connected = true;
      connection.end('{"ok":true}\n');
    }, async (socketPath) => {
      const result = await runClient([action, ...requiredArgs, '--socket', socketPath]);
      assert.equal(result.code, 2);
      assert.match(result.stdout, /"code":"usage"/);
      assert.equal(result.stderr, 'craft-ops-client: usage\n');
      assert.equal(connected, false);
    });
  });
}

test('settles when one complete response frame arrives without waiting for EOF', async () => {
  await withServer((connection) => {
    connection.once('data', () => connection.write('{"ok":true,"contracts":[]}\n'));
  }, async (socketPath) => {
    const result = await callBroker(
      {
        version: '1',
        requestId: '7be5d5a3-bfbe-45eb-9d41-718fa5ea2bfb',
        action: 'contracts',
        input: {},
        dryRun: false,
      },
      { socketPath, timeoutMs: 100 },
    );
    assert.deepEqual(result, { ok: true, contracts: [] });
  });
});

test('rejects a second response frame already buffered', async () => {
  await withServer((connection) => {
    connection.once('data', () => connection.end('{"ok":true}\n{"ok":true}\n'));
  }, async (socketPath) => {
    await assert.rejects(
      () =>
        callBroker(
          {
            version: '1',
            requestId: '7be5d5a3-bfbe-45eb-9d41-718fa5ea2bfb',
            action: 'contracts',
            input: {},
            dryRun: false,
          },
          { socketPath, timeoutMs: 100 },
        ),
      /one newline-delimited JSON object|trailing bytes/i,
    );
  });
});

test('rejects close without a complete response frame', async () => {
  await withServer((connection) => {
    connection.once('data', () => {
      connection.write('{"ok":true');
      connection.destroy();
    });
  }, async (socketPath) => {
    await assert.rejects(
      () =>
        callBroker(
          {
            version: '1',
            requestId: '7be5d5a3-bfbe-45eb-9d41-718fa5ea2bfb',
            action: 'contracts',
            input: {},
            dryRun: false,
          },
          { socketPath, timeoutMs: 100 },
        ),
      /complete response frame|newline-delimited/i,
    );
  });
});

test('rejects unknown and conflicting request fields before connecting', () => {
  assert.throws(
    () =>
      validateRequest({
        version: '1',
        requestId: '7be5d5a3-bfbe-45eb-9d41-718fa5ea2bfb',
        action: 'resume',
        contract: 'meeting-ingest',
        runId: 'run-1',
        input: {},
        dryRun: false,
        unsafe: true,
      }),
    /unknown.*field|only valid for run/i,
  );
});

test('rejects a broker response larger than the protocol limit', async () => {
  await withServer((connection) => {
    connection.end(`${JSON.stringify({ ok: true, payload: 'x'.repeat(MAX_MESSAGE_BYTES) })}\n`);
  }, async (socketPath) => {
    await assert.rejects(
      () =>
        callBroker(
          {
            version: '1',
            requestId: '7be5d5a3-bfbe-45eb-9d41-718fa5ea2bfb',
            action: 'contracts',
            input: {},
            dryRun: false,
          },
          { socketPath, timeoutMs: 1_000 },
        ),
      /larger than 4 MiB/i,
    );
  });
});
