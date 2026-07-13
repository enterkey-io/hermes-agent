import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import test from 'node:test';

import {
  BROKER_SOCKET_PATH,
  ClientError,
  RESPONSE_MAX_BYTES,
  TIMEOUT_MS,
  callBroker,
  parseCli,
  validateRequest,
  validateResponse,
} from './gbrain-broker-client.mjs';

const REQUEST_ID = '7be5d5a3-bfbe-45eb-9d41-718fa5ea2bfb';
const request = {
  version: '1',
  request_id: REQUEST_ID,
  operation: 'search',
  source: 'shared_craft',
  params: { query: 'weekly plan', limit: 1 },
};
const sourceResult = {
  ok: true,
  request_id: REQUEST_ID,
  result: { sources: [
    { alias: 'shared_craft', read: true, capture: false },
    { alias: 'shared_meetings', read: true, capture: false },
    { alias: 'shared_federated', read: true, capture: false },
  ] },
};

test('uses only the compiled socket path and fixed protocol caps', () => {
  assert.equal(BROKER_SOCKET_PATH, '/run/user/1000/gbrain-nano-broker/gbrain-nano.sock');
  assert.equal(TIMEOUT_MS, 10_000);
  assert.equal(RESPONSE_MAX_BYTES, 256 * 1024);
  assert.deepEqual(validateRequest(request), request);
});

test('rejects every unsupported or open request surface', () => {
  for (const value of [
    { ...request, version: '2' },
    { ...request, request_id: 'not-a-uuid' },
    { ...request, operation: 'think' },
    { ...request, operation: 'query' },
    { ...request, operation: 'raw_data' },
    { ...request, operation: 'get_version' },
    { ...request, operation: 'source_admin' },
    { ...request, source: 'private_memory' },
    { ...request, source: undefined },
    { ...request, source_id: 'registered-id' },
    { ...request, params: { query: 'weekly plan', limit: 1, raw: true } },
    { ...request, operation: 'get', params: { slug: 'arbitrary/slug' } },
    { ...request, operation: 'get', params: { page_ref: 'ref', path: '/private/path' } },
    { ...request, operation: 'graph', params: { page_ref: 'ref', depth: 3 } },
    { ...request, operation: 'sources', params: { source_admin: true } },
  ]) assert.throws(() => validateRequest(value), ClientError);
});

test('enforces every operation parameter boundary and request cap', () => {
  const base = { version: '1', request_id: REQUEST_ID, source: 'shared_craft' };
  for (const value of [
    { ...base, operation: 'sources', params: { extra: true } },
    { ...base, operation: 'search', params: { query: '', limit: 1 } },
    { ...base, operation: 'search', params: { query: 'x'.repeat(513), limit: 1 } },
    { ...base, operation: 'search', params: { query: 'x', limit: 0 } },
    { ...base, operation: 'search', params: { query: 'x', limit: 11 } },
    { ...base, operation: 'get', params: { page_ref: '' } },
    { ...base, operation: 'get', params: { page_ref: 'x'.repeat(129) } },
    { ...base, operation: 'graph', params: { page_ref: 'ref', depth: 0 } },
    { ...base, operation: 'capture', params: {} },
    { ...base, operation: 'capture', params: { fact: [] } },
    { ...base, operation: 'capture', params: { fact: { statement: 'x'.repeat(33 * 1024) } } },
  ]) assert.throws(() => validateRequest(value), ClientError);
});

test('rejects socket overrides, duplicate flags, malformed params, and trailing CLI arguments', () => {
  for (const args of [
    ['search', '--source', 'shared_craft', '--params', '{}', '--socket', '/tmp/anything'],
    ['search', '--source', 'shared_craft', '--source', 'shared_meetings', '--params', '{}'],
    ['search', '--source', 'shared_craft', '--params', '{}', '--params', '{}'],
    ['search', '--source', 'shared_craft', '--params', '{'],
    ['search', '--source'],
    ['search', '--source', 'shared_craft', 'trailing'],
  ]) assert.throws(() => parseCli(args), ClientError);
});

test('validates closed nested results and request-id correlation', () => {
  assert.deepEqual(validateResponse(sourceResult, 'sources', REQUEST_ID, 'shared_craft', {}), sourceResult);
  const search = {
    ok: true,
    request_id: REQUEST_ID,
    result: { hits: [{ page_ref: 'opaque', title: '', source: 'shared_craft', date: '', provenance: 'shared retrieval', excerpt: '' }] },
  };
  assert.deepEqual(validateResponse(search, 'search', REQUEST_ID, 'shared_craft', { query: 'weekly', limit: 1 }), search);
  const page = {
    ok: true,
    request_id: REQUEST_ID,
    result: { page_ref: 'opaque', title: '', source: 'shared_craft', provenance: 'shared page', content: '' },
  };
  assert.deepEqual(validateResponse(page, 'get', REQUEST_ID, 'shared_craft', { page_ref: 'opaque' }), page);
  const graph = {
    ok: true,
    request_id: REQUEST_ID,
    result: {
      page_ref: 'opaque',
      nodes: [{ page_ref: 'opaque', title: 'Node', source: 'shared_craft', depth: 0 }],
      edges: [{ from_ref: 'opaque', to_ref: 'other', type: 'related' }],
    },
  };
  assert.deepEqual(validateResponse(graph, 'graph', REQUEST_ID, 'shared_craft', { page_ref: 'opaque', depth: 1 }), graph);

  for (const [response, operation] of [
    [{ ...sourceResult, request_id: '5c314c61-fb97-4a20-af0c-9df0bb8b8651' }, 'sources'],
    [{ ...sourceResult, leak: 'private' }, 'sources'],
    [{ ...sourceResult, result: { sources: [...sourceResult.result.sources, sourceResult.result.sources[0]] } }, 'sources'],
    [{ ...sourceResult, result: { sources: sourceResult.result.sources.map((entry, index) => index === 0 ? { ...entry, capture: true } : entry) } }, 'sources'],
    [{ ...search, result: { hits: [{ ...search.result.hits[0], slug: 'raw/slug' }] } }, 'search'],
    [{ ...search, result: { hits: [{ ...search.result.hits[0], source: 'shared_meetings' }] } }, 'search'],
    [{ ...search, result: { hits: [search.result.hits[0], search.result.hits[0]] } }, 'search'],
    [{ ...page, result: { ...page.result, content: 'x'.repeat(16 * 1024) } }, 'get'],
    [{ ...page, result: { ...page.result, source: 'shared_meetings' } }, 'get'],
    [{ ...page, result: { ...page.result, page_ref: 'different' } }, 'get'],
    [{ ...graph, result: { ...graph.result, nodes: [{ ...graph.result.nodes[0], slug: 'raw' }] } }, 'graph'],
    [{ ...graph, result: { ...graph.result, nodes: [{ ...graph.result.nodes[0], source: 'shared_meetings' }] } }, 'graph'],
    [{ ...graph, result: { ...graph.result, page_ref: 'different' } }, 'graph'],
    [{ ...graph, result: { ...graph.result, nodes: [{ ...graph.result.nodes[0], depth: 2 }] } }, 'graph'],
    [{ ...graph, result: { ...graph.result, edges: [{ ...graph.result.edges[0], path: '/raw' }] } }, 'graph'],
  ]) {
    const params = operation === 'search'
      ? { query: 'weekly', limit: 1 }
      : operation === 'get'
        ? { page_ref: 'opaque' }
        : operation === 'graph'
          ? { page_ref: 'opaque', depth: 1 }
          : {};
    assert.throws(() => validateResponse(response, operation, REQUEST_ID, 'shared_craft', params), ClientError);
  }
});

test('accepts only closed content-free broker errors and no capture success', () => {
  const denied = { ok: false, request_id: REQUEST_ID, error: { code: 'forbidden', retryable: false } };
  assert.deepEqual(validateResponse(denied, 'capture', REQUEST_ID, 'shared_craft', { fact: {} }), denied);
  for (const response of [
    { ...denied, error: { ...denied.error, message: 'sensitive' } },
    { ...denied, error: { code: 'unknown', retryable: false } },
    { ...denied, error: { code: 'forbidden', retryable: 'false' } },
    { ok: true, request_id: REQUEST_ID, result: { receipt_id: 'x', status: 'captured' } },
  ]) assert.throws(() => validateResponse(response, 'capture', REQUEST_ID, 'shared_craft', { fact: {} }), ClientError);
});

class FakeSocket extends EventEmitter {
  constructor(onRequest) {
    super();
    this.onRequest = onRequest;
    this.destroyed = false;
  }
  end(payload) { this.onRequest(this, Buffer.from(payload)); }
  destroy() { this.destroyed = true; }
}

async function withFakeTransport({ onRequest, lstat }, run) {
  const originalCreateConnection = net.createConnection;
  const originalLstat = fs.lstatSync;
  const socket = new FakeSocket(onRequest ?? (() => {}));
  const paths = [];
  fs.lstatSync = lstat ?? ((target) => {
    paths.push(target);
    const directory = target === path.dirname(BROKER_SOCKET_PATH);
    return {
      uid: process.getuid(),
      gid: process.getgid(),
      mode: directory ? 0o40700 : 0o140600,
      isDirectory: () => directory,
      isSocket: () => !directory,
      isSymbolicLink: () => false,
    };
  });
  net.createConnection = (target) => {
    assert.equal(target, BROKER_SOCKET_PATH);
    queueMicrotask(() => socket.emit('connect'));
    return socket;
  };
  try {
    return await run({ socket, paths });
  } finally {
    net.createConnection = originalCreateConnection;
    fs.lstatSync = originalLstat;
  }
}

test('sends one strict frame, half-closes, and waits for one complete response', async () => {
  await withFakeTransport({
    onRequest(socket, payload) {
      assert.equal(payload.toString(), `${JSON.stringify({ ...request, operation: 'sources', params: {} })}\n`);
      queueMicrotask(() => {
        socket.emit('data', Buffer.from(`${JSON.stringify(sourceResult)}\n`));
        socket.emit('end');
      });
    },
  }, async ({ paths }) => {
    const result = await callBroker({ ...request, operation: 'sources', params: {} });
    assert.deepEqual(result, sourceResult);
    assert.deepEqual(paths, [path.dirname(BROKER_SOCKET_PATH), BROKER_SOCKET_PATH]);
  });
});

test('rejects malformed UTF-8, malformed JSON, partial, multiple, trailing, and oversized responses', async () => {
  const frames = [
    [Buffer.from([0x7b, 0xc3, 0x7d, 0x0a])],
    [Buffer.from('{\n')],
    [Buffer.from('{"ok":true')],
    [Buffer.from(`${JSON.stringify(sourceResult)}\n${JSON.stringify(sourceResult)}\n`)],
    [Buffer.from(`${JSON.stringify(sourceResult)}\nx`)],
    [Buffer.alloc(RESPONSE_MAX_BYTES + 1, 0x78)],
  ];
  for (const chunks of frames) {
    await withFakeTransport({
      onRequest(socket) {
        queueMicrotask(() => {
          for (const chunk of chunks) socket.emit('data', chunk);
          socket.emit('end');
        });
      },
    }, async () => {
      await assert.rejects(callBroker({ ...request, operation: 'sources', params: {} }), ClientError);
    });
  }
});

test('rejects a delayed second response frame', async () => {
  await withFakeTransport({
    onRequest(socket) {
      socket.emit('data', Buffer.from(`${JSON.stringify(sourceResult)}\n`));
      setImmediate(() => {
        socket.emit('data', Buffer.from(`${JSON.stringify(sourceResult)}\n`));
        socket.emit('end');
      });
    },
  }, async () => {
    await assert.rejects(callBroker({ ...request, operation: 'sources', params: {} }), ClientError);
  });
});

test('rejects missing, symlinked, non-directory, non-socket, wrong-owner, and wrong-mode paths before connect', async () => {
  const parent = path.dirname(BROKER_SOCKET_PATH);
  const cases = [
    () => { throw Object.assign(new Error('missing'), { code: 'ENOENT' }); },
    (target) => ({ uid: process.getuid(), gid: process.getgid(), mode: 0o40700, isDirectory: () => true, isSocket: () => false, isSymbolicLink: () => target === parent }),
    (target) => ({ uid: process.getuid(), gid: process.getgid(), mode: target === parent ? 0o40700 : 0o140600, isDirectory: () => target === parent, isSocket: () => target !== parent, isSymbolicLink: () => target !== parent }),
    () => ({ uid: process.getuid(), gid: process.getgid(), mode: 0o100600, isDirectory: () => false, isSocket: () => false, isSymbolicLink: () => false }),
    (target) => ({ uid: process.getuid(), gid: process.getgid(), mode: target === parent ? 0o40700 : 0o100600, isDirectory: () => target === parent, isSocket: () => false, isSymbolicLink: () => false }),
    (target) => ({ uid: process.getuid() + 1, gid: process.getgid(), mode: target === parent ? 0o40700 : 0o140600, isDirectory: () => target === parent, isSocket: () => target !== parent, isSymbolicLink: () => false }),
    (target) => ({ uid: process.getuid(), gid: process.getgid() + 1, mode: target === parent ? 0o40700 : 0o140600, isDirectory: () => target === parent, isSocket: () => target !== parent, isSymbolicLink: () => false }),
    (target) => ({ uid: process.getuid(), gid: process.getgid(), mode: target === parent ? 0o40755 : 0o140600, isDirectory: () => target === parent, isSocket: () => target !== parent, isSymbolicLink: () => false }),
    (target) => ({ uid: process.getuid(), gid: process.getgid(), mode: target === parent ? 0o40700 : 0o140660, isDirectory: () => target === parent, isSocket: () => target !== parent, isSymbolicLink: () => false }),
  ];
  for (const lstat of cases) {
    let connected = false;
    await withFakeTransport({ lstat, onRequest: () => { connected = true; } }, async () => {
      await assert.rejects(callBroker({ ...request, operation: 'sources', params: {} }), ClientError);
      assert.equal(connected, false);
    });
  }
});

test('classifies connect failure, incomplete close, and timeout without reflecting data', async () => {
  await withFakeTransport({ onRequest(socket) { queueMicrotask(() => socket.emit('error', new Error('private connection detail'))); } }, async () => {
    await assert.rejects(callBroker({ ...request, operation: 'sources', params: {} }), (error) => error.code === 'unavailable' && !error.message.includes('private'));
  });
  await withFakeTransport({ onRequest(socket) { queueMicrotask(() => { socket.emit('data', Buffer.from('{"ok":')); socket.emit('close'); }); } }, async () => {
    await assert.rejects(callBroker({ ...request, operation: 'sources', params: {} }), (error) => error.code === 'unavailable');
  });
  const originalSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (callback, _delay, ...args) => originalSetTimeout(callback, 5, ...args);
  try {
    await withFakeTransport({}, async () => {
      await assert.rejects(callBroker({ ...request, operation: 'sources', params: {} }), (error) => error.code === 'timeout');
    });
  } finally {
    globalThis.setTimeout = originalSetTimeout;
  }
});
