#!/usr/bin/env node
import crypto from 'node:crypto';
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const BROKER_SOCKET_PATH = '/run/user/1000/gbrain-nano-broker/gbrain-nano.sock';
export const REQUEST_MAX_BYTES = 32 * 1024;
export const RESPONSE_MAX_BYTES = 256 * 1024;
export const TIMEOUT_MS = 10_000;

const OPERATIONS = new Set(['sources', 'search', 'get', 'graph', 'capture']);
const SOURCES = new Set(['shared_craft', 'shared_meetings', 'shared_federated']);
const ERROR_CODES = new Set(['invalid_request', 'forbidden', 'unavailable', 'timeout', 'rate_limited', 'not_found', 'conflict', 'upstream_failure', 'internal']);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export class ClientError extends Error {
  constructor(code) { super(code); this.code = code; }
}

function invalid() { throw new ClientError('invalid_request'); }
function object(value) { if (value === null || typeof value !== 'object' || Array.isArray(value)) invalid(); return value; }
function exact(value, fields) { for (const field of Object.keys(value)) if (!fields.includes(field)) invalid(); }
function text(value, min, max) { if (typeof value !== 'string' || Buffer.byteLength(value, 'utf8') < min || Buffer.byteLength(value, 'utf8') > max) invalid(); return value; }
function displayText(value, min, max) { if (typeof value !== 'string' || Array.from(value).length < min || Array.from(value).length > max) invalid(); return value; }
function integer(value, min, max) { if (!Number.isInteger(value) || value < min || value > max) invalid(); return value; }

export function validateRequest(value) {
  const request = object(value);
  exact(request, ['version', 'request_id', 'operation', 'source', 'params']);
  if (request.version !== '1' || typeof request.request_id !== 'string' || !UUID.test(request.request_id)) invalid();
  if (!OPERATIONS.has(request.operation) || !SOURCES.has(request.source)) invalid();
  const params = object(request.params);
  if (request.operation === 'sources') exact(params, []);
  if (request.operation === 'search') { exact(params, ['query', 'limit']); text(params.query, 1, 512); integer(params.limit, 1, 10); }
  if (request.operation === 'get') { exact(params, ['page_ref']); text(params.page_ref, 1, 128); }
  if (request.operation === 'graph') { exact(params, ['page_ref', 'depth']); text(params.page_ref, 1, 128); integer(params.depth, 1, 2); }
  if (request.operation === 'capture') { exact(params, ['fact']); object(params.fact); }
  if (Buffer.byteLength(JSON.stringify(request), 'utf8') > REQUEST_MAX_BYTES) invalid();
  return request;
}

export function validateResponse(value, operation, expectedRequestId, expectedSource, expectedParams) {
  const response = object(value);
  if (!SOURCES.has(expectedSource)) invalid();
  exact(response, ['ok', 'request_id', ...(response.ok === true ? ['result'] : ['error'])]);
  if (typeof response.ok !== 'boolean' || (response.request_id !== null && (typeof response.request_id !== 'string' || !UUID.test(response.request_id)))) invalid();
  if (expectedRequestId && response.request_id !== null && response.request_id !== expectedRequestId) invalid();
  if (response.ok && response.request_id !== expectedRequestId) invalid();
  if (response.ok) { object(response.result); if (operation) validateResult(response.result, operation, expectedSource, object(expectedParams)); return response; }
  const error = object(response.error);
  exact(error, ['code', 'retryable']);
  if (!ERROR_CODES.has(error.code) || typeof error.retryable !== 'boolean') invalid();
  const retryable = error.code === 'unavailable' || error.code === 'timeout' || error.code === 'upstream_failure';
  if (error.retryable !== retryable) invalid();
  return response;
}

function validateResult(result, operation, expectedSource, expectedParams) {
  if (operation === 'sources') {
    exact(result, ['sources']);
    if (!Array.isArray(result.sources) || result.sources.length !== 3) invalid();
    const aliases = new Set();
    for (const source of result.sources) { const entry = object(source); exact(entry, ['alias', 'read', 'capture']); if (!SOURCES.has(entry.alias) || entry.read !== true || entry.capture !== false || aliases.has(entry.alias)) invalid(); aliases.add(entry.alias); }
    if (aliases.size !== SOURCES.size) invalid();
  } else if (operation === 'search') {
    exact(result, ['hits']);
    if (!Array.isArray(result.hits) || result.hits.length > expectedParams.limit || result.hits.length > 10) invalid();
    for (const hit of result.hits) { const entry = object(hit); exact(entry, ['page_ref', 'title', 'source', 'date', 'provenance', 'excerpt']); text(entry.page_ref, 1, 128); if (entry.source !== expectedSource) invalid(); displayText(entry.title, 0, 160); if (typeof entry.date !== 'string' || (entry.date !== '' && !/^\d{4}-\d{2}-\d{2}$/.test(entry.date))) invalid(); displayText(entry.provenance, 0, 160); displayText(entry.excerpt, 0, 600); }
  } else if (operation === 'get') {
    exact(result, ['page_ref', 'title', 'source', 'provenance', 'content']); text(result.page_ref, 1, 128); if (result.page_ref !== expectedParams.page_ref) invalid(); displayText(result.title, 0, 160); if (result.source !== expectedSource) invalid(); displayText(result.provenance, 0, 160); if (typeof result.content !== 'string' || Buffer.byteLength(JSON.stringify(result), 'utf8') > 16 * 1024) invalid();
  } else if (operation === 'graph') {
    exact(result, ['page_ref', 'nodes', 'edges']); text(result.page_ref, 1, 128); if (result.page_ref !== expectedParams.page_ref) invalid(); if (!Array.isArray(result.nodes) || !Array.isArray(result.edges) || result.nodes.length > 50 || result.edges.length > 50) invalid();
    for (const node of result.nodes) { const entry = object(node); exact(entry, ['page_ref', 'title', 'source', 'depth']); text(entry.page_ref, 1, 128); displayText(entry.title, 0, 160); if (entry.source !== expectedSource) invalid(); integer(entry.depth, 0, expectedParams.depth); }
    for (const edge of result.edges) { const entry = object(edge); exact(entry, ['from_ref', 'to_ref', 'type']); text(entry.from_ref, 1, 128); text(entry.to_ref, 1, 128); displayText(entry.type, 0, 80); }
    if (Buffer.byteLength(JSON.stringify(result), 'utf8') > 32 * 1024) invalid();
  } else {
    invalid();
  }
}

function assertSocket() {
  let directory;
  let socket;
  try {
    directory = fs.lstatSync(path.dirname(BROKER_SOCKET_PATH));
    socket = fs.lstatSync(BROKER_SOCKET_PATH);
  } catch { throw new ClientError('unavailable'); }
  const uid = process.getuid?.();
  const gid = process.getgid?.();
  if (directory.isSymbolicLink() || !directory.isDirectory() || (directory.mode & 0o777) !== 0o700) throw new ClientError('unavailable');
  if (socket.isSymbolicLink() || !socket.isSocket() || (socket.mode & 0o777) !== 0o600) throw new ClientError('unavailable');
  if (uid !== undefined && (directory.uid !== uid || socket.uid !== uid)) throw new ClientError('unavailable');
  if (gid !== undefined && (directory.gid !== gid || socket.gid !== gid)) throw new ClientError('unavailable');
}

export async function callBroker(request) {
  const validated = validateRequest(request);
  assertSocket();
  const payload = Buffer.from(`${JSON.stringify(validated)}\n`, 'utf8');
  if (payload.length > REQUEST_MAX_BYTES) invalid();
  return await new Promise((resolve, reject) => {
    let settled = false;
    let response = Buffer.alloc(0);
    let socket;
    const settle = (fn, value) => { if (settled) return; settled = true; clearTimeout(timer); fn(value); socket?.destroy(); };
    const timer = setTimeout(() => settle(reject, new ClientError('timeout')), TIMEOUT_MS);
    try { socket = net.createConnection(BROKER_SOCKET_PATH); }
    catch { settle(reject, new ClientError('unavailable')); return; }
    socket.once('connect', () => socket.end(payload));
    socket.on('data', (chunk) => {
      response = Buffer.concat([response, chunk]);
      if (response.length > RESPONSE_MAX_BYTES) return settle(reject, new ClientError('invalid_request'));
      const newline = response.indexOf(0x0a);
      if (newline === -1) return;
      if (newline !== response.length - 1) return settle(reject, new ClientError('invalid_request'));
    });
    socket.once('end', () => {
      try {
        if (response.length === 0 || response.at(-1) !== 0x0a || response.subarray(0, -1).includes(0x0a)) invalid();
        const decoded = new TextDecoder('utf-8', { fatal: true }).decode(response.subarray(0, -1));
        settle(resolve, validateResponse(JSON.parse(decoded), validated.operation, validated.request_id, validated.source, validated.params));
      } catch { settle(reject, new ClientError('invalid_request')); }
    });
    socket.once('error', () => { if (!settled) settle(reject, new ClientError('unavailable')); });
    socket.once('close', () => { if (!settled) settle(reject, new ClientError('unavailable')); });
  });
}

export function parseCli(argv) {
  const [operation, ...rest] = argv;
  const values = { params: {} };
  const seen = new Set();
  for (let index = 0; index < rest.length; index += 1) {
    const flag = rest[index];
    if (flag === '--socket') throw new ClientError('invalid_request');
    if (!['--source', '--params'].includes(flag) || index + 1 >= rest.length) invalid();
    if (seen.has(flag)) invalid();
    seen.add(flag);
    const raw = rest[++index];
    if (flag === '--source') values.source = raw;
    if (flag === '--params') { try { values.params = JSON.parse(raw); } catch { invalid(); } }
  }
  return validateRequest({ version: '1', request_id: crypto.randomUUID(), operation, source: values.source, params: values.params });
}

export async function main(argv = process.argv.slice(2)) {
  try {
    const response = await callBroker(parseCli(argv));
    process.stdout.write(`${JSON.stringify(response)}\n`);
    if (!response.ok) process.exitCode = 6;
  } catch (error) {
    const code = error instanceof ClientError ? error.code : 'internal';
    process.stdout.write(`${JSON.stringify({ ok: false, request_id: null, error: { code, retryable: code === 'unavailable' || code === 'timeout' } })}\n`);
    process.stderr.write(`gbrain-broker-client: ${code}\n`);
    process.exitCode = code === 'invalid_request' ? 2 : code === 'timeout' ? 4 : 3;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main();
