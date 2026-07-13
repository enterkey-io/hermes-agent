#!/usr/bin/env node
import crypto from 'node:crypto';
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const DEFAULT_SOCKET_PATH = '/run/user/1000/craft-ops/craft-ops.sock';
export const HOST_PREFLIGHT_SOCKET_PATH = '/run/user/1000/craft-ops/craft-ops.sock';
export const DEFAULT_TIMEOUT_MS = 900_000;
export const MAX_MESSAGE_BYTES = 4 * 1024 * 1024;

const ACTIONS = new Set(['contracts', 'run', 'resume', 'reconcile', 'rollback']);
const REQUEST_FIELDS = new Set(['version', 'requestId', 'action', 'contract', 'runId', 'input', 'dryRun']);
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export class CraftOpsClientError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

function usage(message) {
  return new CraftOpsClientError('usage', message);
}

function protocol(message) {
  return new CraftOpsClientError('protocol', message);
}

function byteLength(value) {
  return Buffer.byteLength(JSON.stringify(value), 'utf8');
}

export function validateRequest(request) {
  if (request === null || typeof request !== 'object' || Array.isArray(request)) throw usage('request must be an object');
  if (request.version !== '1') throw usage('version must be "1"');
  if (typeof request.requestId !== 'string' || !UUID_PATTERN.test(request.requestId)) throw usage('requestId must be a UUID');
  if (typeof request.action !== 'string' || !ACTIONS.has(request.action)) throw usage('action is not supported');

  const allowedFields = new Set(['version', 'requestId', 'action']);
  if (request.action === 'run') {
    allowedFields.add('contract');
    allowedFields.add('input');
    allowedFields.add('dryRun');
  }
  if (['resume', 'reconcile', 'rollback'].includes(request.action)) allowedFields.add('runId');
  for (const field of Object.keys(request)) {
    if (!REQUEST_FIELDS.has(field) || !allowedFields.has(field)) throw usage(`unknown request field: ${field}`);
  }

  if (request.action === 'run') {
    if (typeof request.contract !== 'string' || request.contract.trim() === '') throw usage('contract is required for run');
    if (request.input === null || typeof request.input !== 'object' || Array.isArray(request.input)) throw usage('input must be an object');
    if (typeof request.dryRun !== 'boolean') throw usage('dryRun must be a boolean');
    if (byteLength(request.input) > MAX_MESSAGE_BYTES) throw usage('input is larger than 4 MiB');
  } else if (['resume', 'reconcile', 'rollback'].includes(request.action)) {
    if (typeof request.runId !== 'string' || !UUID_PATTERN.test(request.runId)) throw usage('runId must be a UUID');
  }

  if (byteLength(request) > MAX_MESSAGE_BYTES) throw usage('request is larger than 4 MiB');
  return request;
}

function assertSocketPath(socketPath) {
  let stat;
  try {
    stat = fs.lstatSync(socketPath);
  } catch (error) {
    if (error && error.code === 'ENOENT') throw new CraftOpsClientError('unavailable', 'Craft broker socket is unavailable');
    throw new CraftOpsClientError('unavailable', 'Craft broker socket cannot be inspected');
  }
  if (stat.isSymbolicLink()) throw protocol('Craft broker socket path must not be a symlink');
  if (!stat.isSocket()) throw protocol('Craft broker socket path is not a socket');
}

export async function callBroker(request, { socketPath = DEFAULT_SOCKET_PATH, timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
  validateRequest(request);
  assertSocketPath(socketPath);
  const payload = `${JSON.stringify(request)}\n`;
  if (Buffer.byteLength(payload, 'utf8') > MAX_MESSAGE_BYTES) throw usage('request is larger than 4 MiB');

  return new Promise((resolve, reject) => {
    let settled = false;
    let connected = false;
    let response = Buffer.alloc(0);
    const settle = (fn, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      fn(value);
    };
    const socket = net.createConnection({ path: socketPath });
    const rejectAndClose = (error) => {
      settle(reject, error);
      socket.destroy();
    };
    const resolveAndClose = (value) => {
      settle(resolve, value);
      socket.destroy();
    };
    const timer = setTimeout(() => {
      rejectAndClose(new CraftOpsClientError('timeout', 'Craft broker timed out'));
    }, timeoutMs);

    socket.once('connect', () => {
      connected = true;
      socket.end(payload);
    });
    socket.on('data', (chunk) => {
      response = Buffer.concat([response, chunk]);
      if (response.length > MAX_MESSAGE_BYTES) {
        rejectAndClose(protocol('broker response is larger than 4 MiB'));
        return;
      }
      const newline = response.indexOf(0x0a);
      if (newline === -1) return;
      if (newline !== response.length - 1) {
        rejectAndClose(protocol('broker response has multiple frames or trailing bytes'));
        return;
      }

      const text = response.subarray(0, newline).toString('utf8');
      let parsed;
      try {
        parsed = JSON.parse(text);
      } catch {
        rejectAndClose(protocol('broker response is not valid JSON'));
        return;
      }
      if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed) || typeof parsed.ok !== 'boolean') {
        rejectAndClose(protocol('broker response must be an object with boolean ok'));
        return;
      }
      resolveAndClose(parsed);
    });
    socket.once('error', (error) => {
      if (error instanceof CraftOpsClientError) return settle(reject, error);
      if (connected) return settle(reject, protocol('broker closed without a complete response frame'));
      settle(reject, new CraftOpsClientError('unavailable', 'Craft broker connection failed'));
    });
    socket.once('end', () => {
      if (!settled) rejectAndClose(protocol('broker closed without a complete response frame'));
    });
    socket.once('close', () => {
      if (!settled) settle(reject, protocol('broker closed without a complete response frame'));
    });
  });
}

function parseCli(argv) {
  const [action, ...rest] = argv;
  if (!action) throw usage('action is required');
  const values = { input: {}, dryRun: false };
  const seen = new Set();
  let socketPath = DEFAULT_SOCKET_PATH;
  let socketOverride = false;
  for (let index = 0; index < rest.length; index += 1) {
    const flag = rest[index];
    if (flag === '--dry-run') {
      if (seen.has(flag)) throw usage('duplicate --dry-run');
      seen.add(flag);
      values.dryRun = true;
      continue;
    }
    if (!['--contract', '--run-id', '--input', '--socket'].includes(flag)) throw usage(`unknown argument: ${flag}`);
    if (seen.has(flag)) throw usage(`duplicate ${flag}`);
    seen.add(flag);
    const value = rest[index + 1];
    if (!value || value.startsWith('--')) throw usage(`${flag} requires a value`);
    index += 1;
    if (flag === '--contract') values.contract = value;
    if (flag === '--run-id') values.runId = value;
    if (flag === '--socket') {
      socketPath = value;
      socketOverride = true;
    }
    if (flag === '--input') {
      try {
        values.input = JSON.parse(value);
      } catch {
        throw usage('--input must be valid JSON');
      }
    }
  }
  if (socketOverride && action !== 'contracts') throw usage('--socket is only valid for contracts');
  if (socketOverride && socketPath !== HOST_PREFLIGHT_SOCKET_PATH) {
    throw usage(`--socket must be ${HOST_PREFLIGHT_SOCKET_PATH}`);
  }
  const request = { version: '1', requestId: crypto.randomUUID(), action };
  if (action === 'run') {
    request.contract = values.contract;
    request.input = values.input;
    request.dryRun = values.dryRun;
  } else {
    if (seen.has('--contract') || seen.has('--input') || seen.has('--dry-run')) throw usage('run options are only valid for run');
    if (['resume', 'reconcile', 'rollback'].includes(action)) request.runId = values.runId;
  }
  return { request: validateRequest(request), socketPath };
}

function exitCode(error) {
  return { usage: 2, unavailable: 3, timeout: 4, protocol: 5, remote: 6 }[error.code] ?? 5;
}

export async function main(argv = process.argv.slice(2)) {
  try {
    const { request, socketPath } = parseCli(argv);
    const result = await callBroker(request, { socketPath });
    process.stdout.write(`${JSON.stringify(result)}\n`);
    if (!result.ok) process.exitCode = 6;
  } catch (error) {
    const clientError = error instanceof CraftOpsClientError ? error : protocol('unexpected client failure');
    process.stdout.write(`${JSON.stringify({ ok: false, error: { code: clientError.code, message: clientError.message } })}\n`);
    process.stderr.write(`craft-ops-client: ${clientError.code}\n`);
    process.exitCode = exitCode(clientError);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main();
}
