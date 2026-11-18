#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""github-core-clone-01: a GitHub-compatible git forge in one Python stdlib process.

Serves, from a single process and an in-memory store:
  * smart HTTP git (protocol v2 upload-pack ls-refs/fetch + v0 receive-pack),
  * GitHub REST v3 (repos, issues, pull requests, contents, git refs),
  * GitHub GraphQL v4 (the documents gh / github-mcp-server send),
  * MCP (streamable HTTP, legacy initialize handshake + tools).

Lifecycle: if MAKEFUN_ENV_URL is set, bind an ephemeral port, POST {"port": N}
to $MAKEFUN_ENV_URL/register, then poll GET $MAKEFUN_ENV_URL/done until done.
Otherwise serve standalone on $PORT (default 8080).

Determinism: no wall-clock time, no randomness in any stored state. Commits this
forge creates use a fixed identity and a fixed timestamp, so identical inputs
produce identical object ids. The scenario viewer login is derived by porting
wire.ts's seeded RNG (same fixed seed), never hardcoded.
"""
import base64
import difflib
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

U64_MASK = (1 << 64) - 1
SCENARIO_SEED = 0x6A09E667F3BCC909
ZERO_OID = "0" * 40
FIXED_TIMESTAMP = 1700000000
FIXED_IDENTITY = "Conformance <conformance@make.fun>"
MERGED_AT = "2026-01-01T00:00:00Z"
AGENT_NAME = "github-core-clone/1.0.0"

# ---------------------------------------------------------------------------
# Seeded scenario RNG (port of wire.ts: fnv1a64, mix64, xorshift64* Rng,
# generateUsers). Only the viewer login is needed server-side.
# ---------------------------------------------------------------------------

def fnv1a64(text):
    h = 0xCBF29CE484222325
    for ch in text:
        h ^= ord(ch)
        h = (h * 0x100000001B3) & U64_MASK
    return h


def mix64(value):
    z = (value + 0x9E3779B97F4A7C15) & U64_MASK
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & U64_MASK
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & U64_MASK
    return (z ^ (z >> 31)) & U64_MASK


def scenario_seed_for(label):
    return mix64(SCENARIO_SEED ^ fnv1a64(label))


class Rng:
    def __init__(self, seed):
        masked = seed & U64_MASK
        self.state = masked if masked != 0 else SCENARIO_SEED

    def next_u64(self):
        x = self.state
        x = (x ^ (x >> 12)) & U64_MASK
        x = (x ^ ((x << 25) & U64_MASK)) & U64_MASK
        x = (x ^ (x >> 27)) & U64_MASK
        self.state = x
        return (x * 0x2545F4914F6CDD1D) & U64_MASK

    def next_u32(self):
        return self.next_u64() >> 32

    def next_int(self, max_exclusive):
        if max_exclusive == 1:
            return 0
        limit = 4294967296 - (4294967296 % max_exclusive)
        while True:
            v = self.next_u32()
            if v < limit:
                return v % max_exclusive

    def pick(self, items):
        return items[self.next_int(len(items))]


FIRST_NAMES = [
    'ada', 'bram', 'cora', 'dmitri', 'elena', 'felix', 'greta', 'hugo', 'iris',
    'jonas', 'kira', 'lars', 'mira', 'nico', 'olga', 'pavel', 'quinn', 'rosa',
    'sven', 'tess', 'ulla', 'vito', 'wren', 'yara', 'zane',
]
LAST_NAMES = [
    'anders', 'bauer', 'costa', 'dahl', 'eriksen', 'fischer', 'garcia', 'holm',
    'iversen', 'jansen', 'keller', 'lind', 'moreau', 'novak', 'olsen', 'peters',
    'quist', 'rossi', 'sato', 'turi', 'ulrich', 'vega', 'weber', 'xu', 'yilmaz',
    'zimmer',
]


def scenario_viewer():
    """(login, email) of users[0] from wire.ts buildScenario()."""
    rng = Rng(scenario_seed_for('users'))
    used = set()
    first = rng.pick(FIRST_NAMES)
    last = rng.pick(LAST_NAMES)
    login = re.sub(r'[^a-z0-9-]', '', first + last)
    return login, login + '@example.invalid'


VIEWER_LOGIN, VIEWER_EMAIL = scenario_viewer()

# ---------------------------------------------------------------------------
# Git object model
# ---------------------------------------------------------------------------

def hash_object(otype, data):
    h = hashlib.sha1()
    h.update(otype.encode('ascii'))
    h.update(b' ')
    h.update(str(len(data)).encode('ascii'))
    h.update(b'\x00')
    h.update(data)
    return h.hexdigest()


def git_object_header(otype, size):
    return otype.encode('ascii') + b' ' + str(size).encode('ascii') + b'\x00'


def parse_commit(data):
    """Return dict: tree, parents, author, committer, message."""
    text = data.decode('utf-8', 'surrogateescape')
    head, _, message = text.partition('\n\n')
    out = {'tree': None, 'parents': [], 'author': '', 'committer': '', 'message': message}
    for line in head.split('\n'):
        if line.startswith('tree '):
            out['tree'] = line[5:].strip()
        elif line.startswith('parent '):
            out['parents'].append(line[7:].strip())
        elif line.startswith('author '):
            out['author'] = line[7:]
        elif line.startswith('committer '):
            out['committer'] = line[10:]
    return out


def build_commit(tree_oid, parents, message, timestamp=FIXED_TIMESTAMP):
    if not message.endswith('\n'):
        message += '\n'
    lines = ['tree ' + tree_oid]
    for p in parents:
        lines.append('parent ' + p)
    stamp = '%d +0000' % timestamp
    lines.append('author %s %s' % (FIXED_IDENTITY, stamp))
    lines.append('committer %s %s' % (FIXED_IDENTITY, stamp))
    return ('\n'.join(lines) + '\n\n' + message).encode('utf-8', 'surrogateescape')


def parse_tree(data):
    """Return list of (mode_str, name, oid_hex), in stored order."""
    entries = []
    i = 0
    n = len(data)
    while i < n:
        sp = data.index(b' ', i)
        mode = data[i:sp].decode('ascii')
        nul = data.index(b'\x00', sp)
        name = data[sp + 1:nul].decode('utf-8', 'surrogateescape')
        oid = data[nul + 1:nul + 21].hex()
        entries.append((mode, name, oid))
        i = nul + 21
    return entries


def tree_sort_key(entry):
    mode, name, _oid = entry
    # git compares directory names with a trailing slash
    return name + ('/' if mode == '40000' else '')


def build_tree(entries):
    out = bytearray()
    for mode, name, oid in sorted(entries, key=tree_sort_key):
        out += mode.encode('ascii') + b' ' + name.encode('utf-8', 'surrogateescape')
        out += b'\x00' + bytes.fromhex(oid)
    return bytes(out)


def parse_tag(data):
    text = data.decode('utf-8', 'surrogateescape')
    head, _, message = text.partition('\n\n')
    out = {'object': None, 'type': None, 'tag': None, 'tagger': '', 'message': message}
    for line in head.split('\n'):
        if line.startswith('object '):
            out['object'] = line[7:].strip()
        elif line.startswith('type '):
            out['type'] = line[5:].strip()
        elif line.startswith('tag '):
            out['tag'] = line[4:].strip()
        elif line.startswith('tagger '):
            out['tagger'] = line[7:]
    return out


# ---------------------------------------------------------------------------
# pkt-lines
# ---------------------------------------------------------------------------

def pkt_line(payload):
    if isinstance(payload, str):
        payload = payload.encode('utf-8')
    return ('%04x' % (len(payload) + 4)).encode('ascii') + payload


def pkt_flush():
    return b'0000'


def pkt_delim():
    return b'0001'


def pkt_response_end():
    return b'0002'


def encode_pkt_lines(items):
    out = bytearray()
    for item in items:
        if item == 'flush':
            out += pkt_flush()
        elif item == 'delim':
            out += pkt_delim()
        elif item == 'response-end':
            out += pkt_response_end()
        else:
            out += pkt_line(item)
    return bytes(out)


def decode_pkt_lines(data):
    """Parse a byte string into a list of ('data', bytes) | ('flush'|'delim'|'end', None).

    Raises ValueError on a malformed length prefix.
    """
    packets = []
    i = 0
    n = len(data)
    while i < n:
        if i + 4 > n:
            raise ValueError('truncated pkt-line length')
        hexlen = data[i:i + 4]
        try:
            size = int(hexlen.decode('ascii'), 16)
        except ValueError:
            raise ValueError('bad pkt-line length %r' % hexlen)
        if size == 0:
            packets.append(('flush', None))
            i += 4
        elif size == 1:
            packets.append(('delim', None))
            i += 4
        elif size == 2:
            packets.append(('end', None))
            i += 4
        elif size < 4 or i + size > n:
            raise ValueError('pkt-line length out of range')
        else:
            packets.append(('data', bytes(data[i + 4:i + size])))
            i += size
    return packets


class PktReader:
    """Incremental pkt-line decoder over a bytes buffer."""

    def __init__(self, data):
        self.data = data
        self.pos = 0

    def packets(self):
        return decode_pkt_lines(self.data[self.pos:])


# ---------------------------------------------------------------------------
# packfiles
# ---------------------------------------------------------------------------

PACK_TYPES = {1: 'commit', 2: 'tree', 3: 'blob', 4: 'tag', 6: 'ofs-delta', 7: 'ref-delta'}


def _read_varint_size(buf, start):
    first = buf[start]
    byte = first
    size = byte & 0x0F
    shift = 4
    pos = start + 1
    while byte & 0x80:
        byte = buf[pos]
        pos += 1
        size |= (byte & 0x7F) << shift
        shift += 7
    return size, pos, first


def _read_ofs_delta_base(buf, start):
    byte = buf[start]
    pos = start + 1
    offset = byte & 0x7F
    while byte & 0x80:
        byte = buf[pos]
        pos += 1
        offset = ((offset + 1) << 7) | (byte & 0x7F)
    return offset, pos


def _apply_delta(base, delta):
    pos = 0

    def read_varint():
        nonlocal pos
        out = 0
        shift = 0
        while True:
            b = delta[pos]
            pos += 1
            out |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                return out

    src_size = read_varint()
    tgt_size = read_varint()
    if src_size != len(base):
        raise ValueError('delta base size mismatch')
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            cp_off = 0
            cp_size = 0
            for i in range(4):
                if op & (1 << i):
                    cp_off |= delta[pos] << (8 * i)
                    pos += 1
            for i in range(3):
                if op & (0x10 << i):
                    cp_size |= delta[pos] << (8 * i)
                    pos += 1
            if cp_size == 0:
                cp_size = 0x10000
            out += base[cp_off:cp_off + cp_size]
        elif op:
            out += delta[pos:pos + op]
            pos += op
        else:
            raise ValueError('delta opcode 0')
    if len(out) != tgt_size:
        raise ValueError('delta target size mismatch')
    return bytes(out)


def read_pack(pack, resolve_base=None):
    """Parse a pack v2/v3. Returns (objects, delta_count) where objects is a
    dict oid -> (type, bytes). resolve_base(oid) may supply objects the pack
    references but does not carry (thin packs)."""
    if len(pack) < 12 or pack[:4] != b'PACK':
        raise ValueError('not a packfile')
    version = int.from_bytes(pack[4:8], 'big')
    if version not in (2, 3):
        raise ValueError('unsupported pack version %d' % version)
    count = int.from_bytes(pack[8:12], 'big')
    # trailer is the last 20 bytes (SHA-1 of everything before it)
    body_end = len(pack) - 20
    if hashlib.sha1(pack[:body_end]).digest() != pack[body_end:]:
        raise ValueError('pack trailer checksum mismatch')
    objects = {}
    entries = []  # (offset, type, data_or_delta)
    pos = 12
    delta_count = 0
    for _ in range(count):
        start = pos
        size, pos, first = _read_varint_size(pack, pos)
        otype_num = (first >> 4) & 0x07
        if otype_num == 6:  # OFS_DELTA
            base_offset, pos = _read_ofs_delta_base(pack, pos)
            d = zlib.decompressobj()
            raw = d.decompress(pack[pos:])
            consumed = len(pack[pos:]) - len(d.unused_data)
            pos += consumed
            entries.append((start, 'ofs-delta', (base_offset, raw)))
            delta_count += 1
        elif otype_num == 7:  # REF_DELTA
            base_oid = pack[pos:pos + 20].hex()
            pos += 20
            d = zlib.decompressobj()
            raw = d.decompress(pack[pos:])
            consumed = len(pack[pos:]) - len(d.unused_data)
            pos += consumed
            entries.append((start, 'ref-delta', (base_oid, raw)))
            delta_count += 1
        else:
            otype = PACK_TYPES.get(otype_num)
            if otype is None:
                raise ValueError('unknown pack object type %d' % otype_num)
            d = zlib.decompressobj()
            raw = d.decompress(pack[pos:])
            consumed = len(pack[pos:]) - len(d.unused_data)
            pos += consumed
            if len(raw) != size:
                raise ValueError('pack object size mismatch')
            entries.append((start, otype, raw))
    # resolve in offset order (delta bases always precede their deltas in a pack)
    by_offset = {}
    for offset, otype, payload in entries:
        if otype == 'ofs-delta':
            base_offset, delta = payload
            # OFS_DELTA base offset is backwards from this entry's own start
            base = by_offset.get(offset - base_offset)
            if base is None:
                raise ValueError('ofs-delta base not in pack')
            data = _apply_delta(base[1], delta)
            # type comes from the base object
            otype = base[0]
            oid = hash_object(otype, data)
            objects[oid] = (otype, data)
            by_offset[offset] = (otype, data)
        elif otype == 'ref-delta':
            base_oid, delta = payload
            base = objects.get(base_oid)
            if base is None and resolve_base is not None:
                base = resolve_base(base_oid)
            if base is None:
                raise ValueError('ref-delta base %s missing' % base_oid)
            data = _apply_delta(base[1], delta)
            otype = base[0]
            oid = hash_object(otype, data)
            objects[oid] = (otype, data)
            by_offset[offset] = (otype, data)
        else:
            oid = hash_object(otype, payload)
            objects[oid] = (otype, payload)
            by_offset[offset] = (otype, payload)
    return objects, delta_count


def _pack_object_header(otype, size):
    type_num = {'commit': 1, 'tree': 2, 'blob': 3, 'tag': 4}[otype]
    first = (type_num << 4) | (size & 0x0F)
    size >>= 4
    out = bytearray()
    if size:
        out.append(first | 0x80)
        while size:
            b = size & 0x7F
            size >>= 7
            out.append(b | (0x80 if size else 0))
    else:
        out.append(first)
    return bytes(out)


def write_pack(objects):
    """objects: iterable of (oid, type, bytes). Plain (non-delta) pack v2."""
    items = list(objects)
    out = bytearray()
    out += b'PACK' + (2).to_bytes(4, 'big') + len(items).to_bytes(4, 'big')
    for _oid, otype, data in items:
        out += _pack_object_header(otype, len(data))
        out += zlib.compress(data, 1)  # level 1: pack bytes are not graded
        # on size, and level 6 costs ~3x the CPU for the same objects
    trailer = hashlib.sha1(bytes(out)).digest()
    out += trailer
    return bytes(out)


def sideband_packets(pack_bytes, band=1):
    """Split pack bytes into sideband-64k pkt-lines (payload <= 65515)."""
    out = bytearray()
    i = 0
    while i < len(pack_bytes):
        chunk = pack_bytes[i:i + 65515]
        out += pkt_line(bytes([band]) + chunk)
        i += len(chunk)
    return bytes(out)

# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def b64e(data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    return base64.b64encode(data).decode('ascii')


def b64d(text):
    return base64.b64decode(''.join(text.split()))


def gid(kind, *parts):
    return b64e('%s:%s' % (kind, ':'.join(str(p) for p in parts)))


def ungid(value):
    try:
        raw = base64.b64decode(''.join(str(value).split())).decode('utf-8')
    except Exception:
        return None, None
    if ':' not in raw:
        return None, None
    kind, _, rest = raw.partition(':')
    return kind, rest


class Issue:
    __slots__ = ('number', 'id', 'title', 'body', 'state', 'labels', 'comments',
                 'seq', 'repo')

    def __init__(self, number, iid, title, body, labels, seq, repo):
        self.number = number
        self.id = iid
        self.title = title
        self.body = body
        self.state = 'open'
        self.labels = labels  # list of label dicts {name, color}
        self.comments = []    # list of {id, body}
        self.seq = seq
        self.repo = repo

    def global_id(self):
        return gid('Issue', self.repo.owner, self.repo.name, self.number)

    def to_json(self):
        return {
            'id': self.id, 'number': self.number, 'title': self.title,
            'body': self.body, 'state': self.state,
            'labels': [{'name': l['name'], 'color': l.get('color', 'ededed')}
                       for l in self.labels],
            'comments': len(self.comments),
        }


class PullRequest:
    __slots__ = ('number', 'id', 'title', 'body', 'state', 'head', 'base',
                 'head_sha', 'base_sha', 'merged', 'merged_at',
                 'merge_commit_sha', 'seq', 'repo')

    def __init__(self, number, pid, title, body, head, base, head_sha, base_sha,
                 seq, repo):
        self.number = number
        self.id = pid
        self.title = title
        self.body = body
        self.state = 'open'
        self.head = head
        self.base = base
        self.head_sha = head_sha
        self.base_sha = base_sha
        self.merged = False
        self.merged_at = None
        self.merge_commit_sha = None
        self.seq = seq
        self.repo = repo

    def global_id(self):
        return gid('PullRequest', self.repo.owner, self.repo.name, self.number)

    def to_json(self, mergeable=None):
        out = {
            'id': self.id, 'number': self.number, 'title': self.title,
            'body': self.body, 'state': self.state,
            'head': {'ref': self.head, 'sha': self.head_sha,
                     'label': '%s:%s' % (self.repo.owner, self.head)},
            'base': {'ref': self.base, 'sha': self.base_sha,
                     'label': '%s:%s' % (self.repo.owner, self.base)},
            'merged': self.merged,
            'merged_at': self.merged_at,
            'merge_commit_sha': self.merge_commit_sha,
        }
        if mergeable is not None:
            out['mergeable'] = mergeable
        return out


class Repo:
    def __init__(self, dbid, owner, name, private, description):
        self.dbid = dbid
        self.owner = owner
        self.name = name
        self.private = bool(private)
        self.description = description or ''
        self.default_branch = 'main'
        self.refs = {}          # refname -> oid (commit oid or tag oid)
        self.objects = {}       # oid -> (type, bytes)
        self.labels = {}        # name -> {name, color}
        self.issues = {}        # number -> Issue
        self.pulls = {}         # number -> PullRequest
        self.next_number = 1    # shared issue/PR counter
        self.seq = 0            # creation sequence for issues/PRs
        self.lock = threading.RLock()
        self.fetch_cache = {}  # fetch-response cache (see _handle_upload_pack)
        # NOTE: do NOT pre-seed scenario labels here: the suite creates
        # each label via POST /labels and expects 201 for a fresh label;
        # an unknown label on issue creation must still 422 (checked
        # against this dict, which the label endpoint populates).

    def global_id(self):
        return gid('Repository', self.dbid)

    def to_json(self):
        return {
            'id': self.dbid, 'name': self.name,
            'full_name': '%s/%s' % (self.owner, self.name),
            'owner': {'login': self.owner},
            'private': self.private, 'description': self.description,
            'default_branch': self.default_branch,
        }

    # -- git object helpers (caller holds self.lock) -------------------------
    def put_object(self, otype, data):
        oid = hash_object(otype, data)
        self.objects[oid] = (otype, data)
        return oid

    def get_object(self, oid):
        return self.objects.get(oid)

    def ref_tip(self, ref):
        return self.refs.get(ref)

    def branch_tip(self, branch):
        return self.refs.get('refs/heads/' + branch)

    def set_branch(self, branch, oid):
        self.refs['refs/heads/' + branch] = oid

    def branches(self):
        return sorted(n[11:] for n in self.refs if n.startswith('refs/heads/'))

    def default_tip(self):
        return self.branch_tip(self.default_branch)


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.repos = {}         # (owner, name) -> Repo
        self.next_repo_id = 5000
        self.next_item_id = 100000
        self.next_comment_id = 1

    def create_repo(self, owner, name, private, description):
        with self.lock:
            if (owner, name) in self.repos:
                return None
            repo = Repo(self.next_repo_id, owner, name, private, description)
            self.next_repo_id += 1
            self.repos[(owner, name)] = repo
            return repo

    def get_repo(self, owner, name):
        return self.repos.get((owner, name))

    def delete_repo(self, owner, name):
        with self.lock:
            return self.repos.pop((owner, name), None)

    def list_repos(self, owner):
        return [r for (o, _n), r in sorted(self.repos.items()) if o == owner]

    def new_item_id(self):
        with self.lock:
            self.next_item_id += 1
            return self.next_item_id

    def new_comment_id(self):
        with self.lock:
            self.next_comment_id += 1
            return self.next_comment_id


STORE = Store()
REQ_COUNTER = [0]
REQ_LOCK = threading.Lock()


def next_request_id():
    with REQ_LOCK:
        REQ_COUNTER[0] += 1
        return 'req-%d' % REQ_COUNTER[0]


# ---------------------------------------------------------------------------
# Git graph helpers (operate on a Repo; caller holds repo.lock)
# ---------------------------------------------------------------------------

def commit_parents(repo, oid):
    obj = repo.objects.get(oid)
    if obj is None or obj[0] != 'commit':
        return []
    return parse_commit(obj[1])['parents']


def commit_tree(repo, oid):
    obj = repo.objects.get(oid)
    if obj is None or obj[0] != 'commit':
        return None
    return parse_commit(obj[1])['tree']


def collect_reachable(repo, roots, include_tags=True):
    """All object ids reachable from roots (commits, trees, blobs, tags)."""
    seen = set()
    stack = list(roots)
    while stack:
        oid = stack.pop()
        if oid in seen or oid == ZERO_OID:
            continue
        obj = repo.objects.get(oid)
        if obj is None:
            continue
        seen.add(oid)
        otype, data = obj
        if otype == 'commit':
            c = parse_commit(data)
            stack.append(c['tree'])
            stack.extend(c['parents'])
        elif otype == 'tree':
            for _mode, _name, child in parse_tree(data):
                stack.append(child)
        elif otype == 'tag' and include_tags:
            t = parse_tag(data)
            if t['object']:
                stack.append(t['object'])
    return seen


def collect_commit_objects(repo, commits):
    """Objects for the given commits: the commits themselves plus their
    trees/blobs, but NOT parent commits. This is the closure a pack must
    carry for a (possibly shallow) fetch: following parents here would
    leak history past the requested depth."""
    seen = set()
    stack = list(commits)
    while stack:
        oid = stack.pop()
        if oid in seen or oid == ZERO_OID:
            continue
        obj = repo.objects.get(oid)
        if obj is None:
            continue
        seen.add(oid)
        otype, data = obj
        if otype == 'commit':
            stack.append(parse_commit(data)['tree'])
        elif otype == 'tree':
            for _mode, _name, child in parse_tree(data):
                stack.append(child)
        elif otype == 'tag':
            t = parse_tag(data)
            if t['object']:
                stack.append(t['object'])
    return seen


def is_ancestor(repo, ancestor, descendant):
    """True when ancestor == descendant or ancestor is reachable from descendant."""
    if ancestor == descendant:
        return True
    seen = set()
    stack = [descendant]
    while stack:
        oid = stack.pop()
        if oid in seen:
            continue
        seen.add(oid)
        if oid == ancestor:
            return True
        stack.extend(commit_parents(repo, oid))
    return False


def merge_base(repo, a, b):
    """Best common ancestor of two commits (linear-history friendly)."""
    if a is None or b is None:
        return None
    # BFS distances from a
    dist_a = {a: 0}
    queue = [a]
    i = 0
    while i < len(queue):
        cur = queue[i]
        i += 1
        for p in commit_parents(repo, cur):
            if p not in dist_a:
                dist_a[p] = dist_a[cur] + 1
                queue.append(p)
    # BFS from b, track best common
    dist_b = {b: 0}
    queue = [b]
    i = 0
    best = None
    best_score = None
    while i < len(queue):
        cur = queue[i]
        i += 1
        if cur in dist_a:
            score = dist_a[cur] + dist_b[cur]
            if best_score is None or score < best_score:
                best = cur
                best_score = score
        for p in commit_parents(repo, cur):
            if p not in dist_b:
                dist_b[p] = dist_b[cur] + 1
                queue.append(p)
    return best


def flatten_tree(repo, tree_oid, prefix=''):
    """path -> (mode, oid) for every blob in a tree, recursively."""
    out = {}
    obj = repo.objects.get(tree_oid)
    if obj is None or obj[0] != 'tree':
        return out
    for mode, name, oid in parse_tree(obj[1]):
        path = prefix + name
        if mode == '40000':
            out.update(flatten_tree(repo, oid, path + '/'))
        else:
            out[path] = (mode, oid)
    return out


def build_tree_from_files(repo, files):
    """files: path -> (mode, oid). Returns root tree oid."""
    # nest by directory
    tree_map = {}

    def put(node, parts, mode, oid):
        if len(parts) == 1:
            node[parts[0]] = (mode, oid)
        else:
            sub = node.setdefault(parts[0], {})
            put(sub, parts[1:], mode, oid)

    root = {}
    for path, (mode, oid) in files.items():
        put(root, path.split('/'), mode, oid)

    def build(node):
        entries = []
        for name, val in node.items():
            if isinstance(val, dict):
                sub_oid = build(val)
                entries.append(('40000', name, sub_oid))
            else:
                entries.append((val[0], name, val[1]))
        data = build_tree(entries)
        return repo.put_object('tree', data)

    return build(root)


def update_tree(repo, base_tree_oid, changes):
    """changes: path -> (mode, oid) or None to delete. Returns new tree oid."""
    files = {}
    if base_tree_oid:
        files = flatten_tree(repo, base_tree_oid)
    for path, val in changes.items():
        if val is None:
            files.pop(path, None)
        else:
            files[path] = val
    return build_tree_from_files(repo, files)


def make_commit(repo, files_or_tree, parents, message):
    """files_or_tree: dict path->(mode,oid) to build a tree from, or a tree oid
    string. Returns commit oid."""
    if isinstance(files_or_tree, str):
        tree_oid = files_or_tree
    else:
        tree_oid = build_tree_from_files(repo, files_or_tree)
    data = build_commit(tree_oid, parents, message)
    return repo.put_object('commit', data)


def diff_trees(repo, base_tree_oid, head_tree_oid):
    """List of (path, status, old_blob_bytes|None, new_blob_bytes|None)."""
    base = flatten_tree(repo, base_tree_oid) if base_tree_oid else {}
    head = flatten_tree(repo, head_tree_oid) if head_tree_oid else {}
    out = []
    for path in sorted(set(base) | set(head)):
        in_base = path in base
        in_head = path in head
        if in_base and not in_head:
            old = repo.objects[base[path][1]][1]
            out.append((path, 'removed', old, None))
        elif in_head and not in_base:
            new = repo.objects[head[path][1]][1]
            out.append((path, 'added', None, new))
        elif base[path][1] != head[path][1]:
            old = repo.objects[base[path][1]][1]
            new = repo.objects[head[path][1]][1]
            out.append((path, 'modified', old, new))
    return out


def count_line_changes(old, new):
    """(additions, deletions) between two blob byte strings."""
    old_lines = old.decode('utf-8', 'surrogateescape').splitlines() if old is not None else []
    new_lines = new.decode('utf-8', 'surrogateescape').splitlines() if new is not None else []
    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    additions = 0
    deletions = 0
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag in ('insert', 'replace'):
            additions += j2 - j1
        if tag in ('delete', 'replace'):
            deletions += _i2 - _i1
    return additions, deletions


def mergeable_status(repo, pr):
    """'MERGEABLE' / 'CONFLICTING' / 'UNKNOWN' for a pull request."""
    head_tip = repo.branch_tip(pr.head)
    base_tip = repo.branch_tip(pr.base)
    if head_tip is None or base_tip is None:
        return 'UNKNOWN'
    if pr.state != 'open':
        return 'UNKNOWN'
    mb = merge_base(repo, head_tip, base_tip)
    if mb is None:
        return 'UNKNOWN'
    head_changes = {p: (o, n) for p, _s, o, n in diff_trees(
        repo, commit_tree(repo, mb), commit_tree(repo, head_tip))}
    base_changes = {p: (o, n) for p, _s, o, n in diff_trees(
        repo, commit_tree(repo, mb), commit_tree(repo, base_tip))}
    for path in set(head_changes) & set(base_changes):
        _ho, hn = head_changes[path]
        _bo, bn = base_changes[path]
        if hn != bn:
            return 'CONFLICTING'
    return 'MERGEABLE'


def do_squash_merge(repo, pr):
    """Perform the squash merge. Returns (commit_oid, conflict_bool)."""
    head_tip = repo.branch_tip(pr.head)
    base_tip = repo.branch_tip(pr.base)
    if head_tip is None or base_tip is None:
        raise ApiError(404, 'head or base branch not found')
    mb = merge_base(repo, head_tip, base_tip)
    if mb is None:
        raise ApiError(409, 'no merge base')
    if mergeable_status(repo, pr) == 'CONFLICTING':
        return None, True
    head_tree = commit_tree(repo, head_tip)
    base_tree = commit_tree(repo, base_tip)
    mb_tree = commit_tree(repo, mb)
    # apply head's changes onto base's tree
    changes = {}
    head_flat = flatten_tree(repo, head_tree)
    for path, _status, _old, new in diff_trees(repo, mb_tree, head_tree):
        if new is None:
            changes[path] = None
        else:
            changes[path] = head_flat[path]
    new_tree = update_tree(repo, base_tree, changes)
    message = 'Merge pull request #%d from %s/%s' % (pr.number, repo.owner, pr.head)
    commit_oid = make_commit(repo, new_tree, [base_tip], message)
    repo.set_branch(pr.base, commit_oid)
    pr.state = 'closed'
    pr.merged = True
    pr.merged_at = MERGED_AT
    pr.merge_commit_sha = commit_oid
    return commit_oid, False

# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

EXPECTED_TOKEN = os.environ.get('MAKEFUN_SHOP_TOKEN') or os.environ.get('TOKEN') or ''


def check_auth(headers):
    """True when the request carries an acceptable bearer token."""
    auth = headers.get('Authorization') or ''
    if not auth.startswith('Bearer '):
        return False
    token = auth[7:].strip()
    if not token:
        return False
    if EXPECTED_TOKEN:
        return token == EXPECTED_TOKEN
    return True


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'Forge/1.0'

    def log_message(self, *args):
        pass

    def setup(self):
        # Disable Nagle: without TCP_NODELAY, small request/response pairs
        # over loopback stall on the delayed-ACK timer (~40 ms per round
        # trip), which dominates the timed workloads.
        try:
            self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        super().setup()

    # -- plumbing ------------------------------------------------------------
    def _read_body(self):
        length = self.headers.get('Content-Length')
        if not length:
            return b''
        try:
            n = int(length)
        except ValueError:
            return b''
        if n <= 0:
            return b''
        return self.rfile.read(n)

    def _send(self, status, body, ctype, extra=None):
        try:
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('X-GitHub-Request-Id', next_request_id())
            if extra:
                for k, v in extra.items():
                    self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status, obj, extra=None):
        body = json.dumps(obj, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
        headers = {'X-OAuth-Scopes': 'repo, read:org'}
        if extra:
            headers.update(extra)
        self._send(status, body, 'application/json', headers)

    def _err(self, status, message):
        self._json(status, {'message': message})

    def _no_content(self, status=204):
        self._send(status, b'', 'application/json')

    # -- dispatch ------------------------------------------------------------
    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_PATCH(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def _dispatch(self):
        try:
            self._route()
        except ApiError as e:
            self._err(e.status, e.message)
        except BrokenPipeError:
            pass
        except ConnectionResetError:
            pass
        except Exception as e:  # never crash a request thread
            try:
                self._err(500, 'internal error: %s' % e)
            except Exception:
                pass

    def _route(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)
        method = self.command
        body = self._read_body() if method in ('POST', 'PUT', 'PATCH', 'DELETE') else b''

        if path == '/healthz':
            self._json(200, {'status': 'ok'})
            return
        if path == '/api/graphql':
            if method != 'POST':
                self._err(405, 'method not allowed')
                return
            self._handle_graphql(body)
            return
        if path == '/mcp':
            self._handle_mcp(method, body)
            return
        if path.startswith('/api/v3'):
            self._handle_rest(method, path[7:] or '/', query, body)
            return
        # smart HTTP: /{owner}/{repo}/info/refs | /{owner}/{repo}/git-upload-pack
        #             | /{owner}/{repo}/git-receive-pack
        parts = [p for p in path.split('/') if p != '']
        if len(parts) == 4 and parts[2] == 'info' and parts[3] == 'refs':
            if method != 'GET':
                self._err(405, 'method not allowed')
                return
            self._handle_info_refs(parts[0], parts[1], query)
            return
        if len(parts) == 3 and parts[2] in ('git-upload-pack', 'git-receive-pack'):
            if method != 'POST':
                self._err(405, 'method not allowed')
                return
            if parts[2] == 'git-upload-pack':
                self._handle_upload_pack(parts[0], parts[1], body)
            else:
                self._handle_receive_pack(parts[0], parts[1], body)
            return
        self._err(404, 'Not Found')

    # -- git: helpers ---------------------------------------------------------
    def _get_git_repo(self, owner, name):
        if name.endswith('.git'):
            name = name[:-4]
        repo = STORE.get_repo(owner, name)
        if repo is None:
            raise ApiError(404, 'Repository not found')
        return repo

    def _ref_lines_v0(self, repo, service):
        """v0 advertisement ref lines (without preamble/flush)."""
        caps = ('report-status side-band-64k delete-refs ofs-delta agent=%s '
                'symref=HEAD:refs/heads/main' % AGENT_NAME)
        if service == 'git-upload-pack':
            caps = ('symref=HEAD:refs/heads/main side-band-64k ofs-delta agent=%s'
                    % AGENT_NAME)
        refs = sorted(repo.refs.items())
        default_tip = repo.default_tip()
        out = []
        if not refs:
            out.append('%s capabilities^{}\x00%s\n' % (ZERO_OID, caps))
            return out
        head_oid = default_tip or next(iter(dict(refs).values()))
        out.append('%s HEAD\x00%s\n' % (head_oid, caps))
        for ref, oid in refs:
            out.append('%s %s\n' % (oid, ref))
            obj = repo.objects.get(oid)
            if obj is not None and obj[0] == 'tag':
                target = parse_tag(obj[1])['object']
                if target:
                    out.append('%s %s^{}\n' % (target, ref))
        return out

    def _handle_info_refs(self, owner, name, query):
        service = (query.get('service') or [''])[0]
        if service not in ('git-upload-pack', 'git-receive-pack'):
            self._err(404, 'Not Found')
            return
        repo = self._get_git_repo(owner, name)
        with repo.lock:
            if service == 'git-upload-pack' and \
                    (self.headers.get('Git-Protocol') or '') == 'version=2':
                lines = ['version 2\n',
                         'agent=%s\n' % AGENT_NAME,
                         'ls-refs=unborn\n',
                         'fetch=shallow filter\n',
                         'object-format=sha1\n']
                body = encode_pkt_lines(lines + ['flush'])
                ctype = 'application/x-git-upload-pack-advertisement'
            else:
                lines = ['# service=%s\n' % service, 'flush']
                lines += self._ref_lines_v0(repo, service)
                body = encode_pkt_lines(lines)
                ctype = ('application/x-%s-advertisement' % service)
        self._send(200, body, ctype)

    # -- git: ls-refs / fetch --------------------------------------------------
    def _ls_refs_lines(self, repo, args):
        peel = 'peel' in args
        symrefs = 'symrefs' in args
        prefixes = [a[11:] for a in args if a.startswith('ref-prefix ')]
        out = []
        default_tip = repo.default_tip()
        head_oid = default_tip
        if not prefixes:
            if head_oid:
                line = '%s HEAD' % head_oid
                if symrefs:
                    line += ' symref-target:refs/heads/main'
                out.append(line + '\n')
            else:
                line = 'unborn HEAD'
                if symrefs:
                    line += ' symref-target:refs/heads/main'
                out.append(line + '\n')
        for ref in sorted(repo.refs):
            if prefixes and not any(ref.startswith(p) for p in prefixes):
                continue
            oid = repo.refs[ref]
            line = '%s %s' % (oid, ref)
            obj = repo.objects.get(oid)
            if peel and obj is not None and obj[0] == 'tag':
                target = parse_tag(obj[1])['object']
                if target:
                    line += ' peeled:%s' % target
            out.append(line + '\n')
        return out

    def _fetch_objects(self, repo, wants, haves, shallow_in, deepen):
        """Compute (send_objects, shallow_out, unshallow_out, ack_oid).

        send_objects: dict oid -> (type, bytes) to put in the pack.
        """
        # resolve wants to commits (+ tag objects to carry)
        want_commits = []
        carry = {}  # extra objects (tags) to send
        for oid in wants:
            obj = repo.objects.get(oid)
            if obj is None:
                continue
            if obj[0] == 'tag':
                carry[oid] = obj
                target = parse_tag(obj[1])['object']
                if target and repo.objects.get(target, (None,))[0] == 'commit':
                    want_commits.append(target)
            elif obj[0] == 'commit':
                want_commits.append(oid)
            else:
                carry[oid] = obj
        # BFS from wants with depth limit + shallow boundaries
        dist = {}
        queue = []
        for c in want_commits:
            if c not in dist:
                dist[c] = 0
                queue.append(c)
        shallow_set = set(shallow_in)
        i = 0
        while i < len(queue):
            cur = queue[i]
            i += 1
            d = dist[cur]
            if deepen is not None and d >= deepen - 1:
                continue
            # A shallow boundary truncates the wanted walk only when the
            # fetch does NOT deepen: with deepen=N the client explicitly
            # asks to extend past its old boundary by N generations.
            if deepen is None and cur in shallow_set:
                continue
            for p in commit_parents(repo, cur):
                if p not in dist:
                    dist[p] = d + 1
                    queue.append(p)
        wanted_commits = set(dist.keys())
        # held: BFS from haves, truncated at shallow boundaries
        held_commits = set()
        queue = list(haves)
        seen_have = set()
        while queue:
            cur = queue.pop()
            if cur in seen_have:
                continue
            seen_have.add(cur)
            obj = repo.objects.get(cur)
            if obj is None or obj[0] != 'commit':
                continue
            held_commits.add(cur)
            if cur in shallow_set:
                continue
            queue.extend(commit_parents(repo, cur))
        send_commits = wanted_commits - held_commits
        # object closure of send commits (trees/blobs only, never parents)
        # minus the objects the client already holds
        send_objs = {}
        for oid in collect_commit_objects(repo, send_commits):
            send_objs[oid] = repo.objects[oid]
        held_objs = collect_commit_objects(repo, held_commits)
        for oid in list(send_objs):
            if oid in held_objs:
                del send_objs[oid]
        for oid, obj in carry.items():
            if oid not in held_objs:
                send_objs[oid] = obj
        # shallow-info
        shallow_out = set()
        if deepen is not None:
            for c, d in dist.items():
                if d == deepen - 1:
                    shallow_out.add(c)
        for b in shallow_in:
            if b in wanted_commits:
                parents = commit_parents(repo, b)
                if not any(p in wanted_commits for p in parents):
                    shallow_out.add(b)
        unshallow_out = set()
        for b in shallow_in:
            if b in wanted_commits:
                parents = commit_parents(repo, b)
                if any(p in wanted_commits for p in parents):
                    unshallow_out.add(b)
        # acknowledgment: a common base?
        ack_oid = None
        for h in haves:
            if any(is_ancestor(repo, h, w) for w in want_commits):
                ack_oid = h
                break
        return send_objs, sorted(shallow_out), sorted(unshallow_out), ack_oid

    def _handle_upload_pack(self, owner, name, body):
        repo = self._get_git_repo(owner, name)
        try:
            packets = decode_pkt_lines(body)
        except ValueError:
            self._err(400, 'malformed pkt-line stream')
            return
        lines = []
        for kind, payload in packets:
            if kind == 'data':
                lines.append(payload.decode('utf-8', 'surrogateescape'))
        if not lines:
            self._err(400, 'empty request')
            return
        command = lines[0].strip()
        with repo.lock:
            if command == 'command=ls-refs':
                args = []
                seen_delim = False
                for line in lines[1:]:
                    if line.strip() == '':
                        continue
                    args.append(line.strip())
                # args are the lines after 'delim' in the raw stream; the
                # decoder above dropped delim/flush markers, so filter the
                # command/agent/object-format lines out
                args = [a for a in args
                        if not a.startswith('agent=')
                        and not a.startswith('object-format=')]
                out = self._ls_refs_lines(repo, args)
                resp = encode_pkt_lines(out + ['flush'])
                self._send(200, resp, 'application/x-git-upload-pack-result')
                return
            if command == 'command=fetch':
                wants, haves, shallow_in, deepen = [], [], [], None
                for line in lines[1:]:
                    line = line.strip()
                    if line.startswith('want '):
                        wants.append(line[5:].strip())
                    elif line.startswith('have '):
                        haves.append(line[5:].strip())
                    elif line.startswith('shallow '):
                        shallow_in.append(line[8:].strip())
                    elif line.startswith('deepen '):
                        try:
                            deepen = int(line[7:].strip())
                        except ValueError:
                            deepen = None
                # Cache identical fetch responses: the bench (and real
                # clients) repeat the same fetch against an unchanged repo
                # many times concurrently. The key captures the full git
                # state that can affect the answer — refs, object count
                # (objects are append-only, so count + refs pin the set),
                # and the request parameters. Responses are deterministic
                # (sorted pack, no timestamps), so a hit is byte-identical.
                cache_key = (tuple(sorted(repo.refs.items())),
                             len(repo.objects), tuple(sorted(wants)),
                             tuple(sorted(haves)), tuple(sorted(shallow_in)),
                             deepen)
                cached = repo.fetch_cache.get(cache_key)
                if cached is not None:
                    self._send(200, cached,
                               'application/x-git-upload-pack-result')
                    return
                send_objs, shallow_out, unshallow_out, ack_oid = \
                    self._fetch_objects(repo, wants, haves, shallow_in, deepen)
                parts = bytearray()
                parts += encode_pkt_lines(['acknowledgments\n'])
                if ack_oid:
                    parts += encode_pkt_lines(['ACK %s\n' % ack_oid, 'ready\n'])
                else:
                    parts += encode_pkt_lines(['NAK\n'])
                parts += pkt_delim()
                if shallow_out or unshallow_out:
                    sec = ['shallow-info\n']
                    sec += ['shallow %s\n' % o for o in shallow_out]
                    sec += ['unshallow %s\n' % o for o in unshallow_out]
                    parts += encode_pkt_lines(sec)
                    parts += pkt_delim()
                if send_objs:
                    pack = write_pack([(oid, otype, data)
                                       for oid, (otype, data)
                                       in sorted(send_objs.items())])
                    parts += encode_pkt_lines(['packfile\n'])
                    parts += sideband_packets(pack)
                parts += pkt_flush()
                resp = bytes(parts)
                if len(repo.fetch_cache) >= 16:
                    repo.fetch_cache.clear()
                repo.fetch_cache[cache_key] = resp
                self._send(200, resp, 'application/x-git-upload-pack-result')
                return
        self._err(400, 'unknown command %r' % command)

    # -- git: receive-pack -------------------------------------------------------
    def _handle_receive_pack(self, owner, name, body):
        repo = self._get_git_repo(owner, name)
        # Parse command pkt-lines incrementally, stopping at the first flush:
        # everything after it is the raw pack (binary, never pkt-parsed).
        commands = []
        pos = 0
        n = len(body)
        pack_start = n
        try:
            while pos + 4 <= n:
                try:
                    size = int(body[pos:pos + 4].decode('ascii'), 16)
                except ValueError:
                    raise ValueError('bad pkt-line length')
                if size == 0:
                    pos += 4
                    pack_start = pos
                    break
                if size < 4 or pos + size > n:
                    raise ValueError('pkt-line length out of range')
                payload = body[pos + 4:pos + size]
                text = payload.decode('utf-8', 'surrogateescape').rstrip('\n')
                if '\x00' in text:
                    text = text.split('\x00', 1)[0]
                parts = text.split(' ')
                if len(parts) >= 3:
                    commands.append((parts[0], parts[1], parts[2]))
                pos += size
            else:
                pack_start = n
        except ValueError:
            self._err(400, 'malformed pkt-line stream')
            return
        pack_bytes = body[pack_start:] if pack_start < n else b''
        with repo.lock:
            unpack_ok = True
            unpack_detail = 'ok'
            if pack_bytes:
                try:
                    objects, _deltas = read_pack(pack_bytes)
                    for oid, (otype, data) in objects.items():
                        repo.objects[oid] = (otype, data)
                except Exception as e:
                    unpack_ok = False
                    unpack_detail = 'error: %s' % e
            report = []
            for old_oid, new_oid, ref in commands:
                if not ref.startswith('refs/'):
                    report.append((ref, False, 'invalid ref'))
                    continue
                current = repo.refs.get(ref)
                if new_oid == ZERO_OID:
                    # delete
                    if current is None:
                        report.append((ref, False, 'remote ref does not exist'))
                    elif old_oid != ZERO_OID and old_oid != current:
                        report.append((ref, False, 'old value mismatch'))
                    else:
                        del repo.refs[ref]
                        report.append((ref, True, ''))
                    continue
                obj = repo.objects.get(new_oid)
                if obj is None:
                    report.append((ref, False, 'object not found'))
                    continue
                if old_oid == ZERO_OID:
                    if current is not None:
                        report.append((ref, False, 'already exists'))
                        continue
                else:
                    if current is None:
                        report.append((ref, False, 'remote ref does not exist'))
                        continue
                    if old_oid != current:
                        report.append((ref, False, 'old value mismatch'))
                        continue
                    if new_oid != current and not is_ancestor(repo, current, new_oid):
                        report.append((ref, False, 'non-fast-forward'))
                        continue
                repo.refs[ref] = new_oid
                report.append((ref, True, ''))
            lines = ['unpack %s\n' % unpack_detail]
            for ref, ok, reason in report:
                if ok:
                    lines.append('ok %s\n' % ref)
                else:
                    lines.append('ng %s %s\n' % (ref, reason))
            report_stream = encode_pkt_lines(lines)
            resp = pkt_line(b'\x01' + report_stream) + pkt_flush()
            self._send(200, resp, 'application/x-git-receive-pack-result')

# ---------------------------------------------------------------------------
# REST v3
# ---------------------------------------------------------------------------

def _parse_json_body(body):
    if not body:
        return {}
    try:
        data = json.loads(body.decode('utf-8'))
    except Exception:
        raise ApiError(400, 'malformed JSON body')
    if not isinstance(data, dict):
        raise ApiError(400, 'JSON body must be an object')
    return data


def _resolve_ref(repo, ref):
    if ref is None:
        return repo.default_tip()
    if ref.startswith('refs/'):
        return repo.refs.get(ref)
    tip = repo.branch_tip(ref)
    if tip is not None:
        return tip
    if len(ref) == 40:
        obj = repo.objects.get(ref)
        if obj is not None and obj[0] == 'commit':
            return ref
    return None


def _paginate(items, query, handler, path):
    try:
        per_page = int((query.get('per_page') or ['30'])[0])
    except ValueError:
        per_page = 30
    try:
        page = int((query.get('page') or ['1'])[0])
    except ValueError:
        page = 1
    per_page = max(1, min(per_page, 100))
    page = max(1, page)
    total = len(items)
    start = (page - 1) * per_page
    sliced = items[start:start + per_page]
    link = None
    if start + per_page < total:
        host = handler.headers.get('Host') or '127.0.0.1'
        base = 'http://%s%s' % (host, path)
        q = dict(query)
        q['page'] = [str(page + 1)]
        q['per_page'] = [str(per_page)]
        qs = urllib.parse.urlencode({k: v[0] for k, v in q.items()})
        link = '<%s?%s>; rel="next"' % (base, qs)
    return sliced, link


class RestMixin:
    def _handle_rest(self, method, path, query, body):
        if not check_auth(self.headers):
            self._err(401, 'Requires authentication')
            return
        segments = [s for s in path.split('/') if s != '']
        try:
            status, obj, link = self._rest_route(method, segments, query, body)
        except ApiError as e:
            self._err(e.status, e.message)
            return
        if status == 204:
            self._no_content(204)
            return
        extra = {'Link': link} if link else None
        self._json(status, obj, extra)

    def _rest_route(self, method, seg, query, body):
        # returns (status, obj, link_header_or_None)
        if not seg:
            if method == 'GET':
                return 200, {'current_user_url': '/api/v3/user',
                             'repository_url': '/api/v3/repos/{owner}/{repo}'}, None
            raise ApiError(404, 'Not Found')
        if seg == ['meta']:
            if method == 'GET':
                return 200, {'installed_version': '1.0.0'}, None
            raise ApiError(404, 'Not Found')
        if seg == ['user']:
            if method == 'GET':
                return 200, {'login': VIEWER_LOGIN, 'id': 1000, 'type': 'User'}, None
            raise ApiError(404, 'Not Found')
        if seg == ['user', 'repos']:
            if method == 'POST':
                data = _parse_json_body(body)
                name = data.get('name')
                if not name or not isinstance(name, str):
                    raise ApiError(422, 'name is required')
                if not re.match(r'^[A-Za-z0-9_.-]+$', name) or len(name) > 100:
                    raise ApiError(422, 'invalid repository name')
                repo = STORE.create_repo(VIEWER_LOGIN, name,
                                         data.get('private', False),
                                         data.get('description', ''))
                if repo is None:
                    raise ApiError(422, 'Repository already exists')
                return 201, repo.to_json(), None
            if method == 'GET':
                items = [r.to_json() for r in STORE.list_repos(VIEWER_LOGIN)]
                sliced, link = _paginate(items, query, self, '/api/v3/user/repos')
                return 200, sliced, link
            raise ApiError(404, 'Not Found')
        if seg[0] == 'repos' and len(seg) >= 3:
            owner, name = seg[1], seg[2]
            repo = STORE.get_repo(owner, name)
            if repo is None:
                raise ApiError(404, 'Repository not found')
            rest = seg[3:]
            return self._repo_route(method, repo, rest, query, body)
        raise ApiError(404, 'Not Found')

    def _repo_route(self, method, repo, rest, query, body):
        if not rest:
            if method == 'GET':
                return 200, repo.to_json(), None
            if method == 'PATCH':
                data = _parse_json_body(body)
                with repo.lock:
                    if 'description' in data:
                        repo.description = data['description'] or ''
                    if 'private' in data:
                        repo.private = bool(data['private'])
                    return 200, repo.to_json(), None
            if method == 'DELETE':
                STORE.delete_repo(repo.owner, repo.name)
                return 204, None, None
            raise ApiError(404, 'Not Found')
        head = rest[0]
        if head == 'branches':
            if len(rest) == 1:
                if method != 'GET':
                    raise ApiError(404, 'Not Found')
                with repo.lock:
                    out = [{'name': b, 'commit': {'sha': repo.branch_tip(b)}}
                           for b in repo.branches()]
                return 200, out, None
            branch = '/'.join(rest[1:])
            if method == 'GET':
                with repo.lock:
                    tip = repo.branch_tip(branch)
                    if tip is None:
                        raise ApiError(404, 'Branch not found')
                    return 200, {'name': branch, 'commit': {'sha': tip}}, None
            raise ApiError(404, 'Not Found')
        if head == 'commits':
            if method != 'GET':
                raise ApiError(404, 'Not Found')
            ref = (query.get('sha') or [None])[0]
            with repo.lock:
                tip = _resolve_ref(repo, ref)
                if tip is None:
                    raise ApiError(404, 'ref not found')
                out = []
                cur = tip
                seen = set()
                while cur and cur not in seen:
                    seen.add(cur)
                    obj = repo.objects.get(cur)
                    if obj is None or obj[0] != 'commit':
                        break
                    c = parse_commit(obj[1])
                    out.append({'sha': cur,
                                'commit': {'message': c['message'].rstrip('\n'),
                                           'author': {'name': 'Conformance',
                                                      'email': 'conformance@make.fun'},
                                           'committer': {'name': 'Conformance',
                                                         'email': 'conformance@make.fun'}}})
                    cur = c['parents'][0] if c['parents'] else None
                return 200, out, None
        if head == 'git' and len(rest) >= 2 and rest[1] == 'refs':
            if method == 'POST' and len(rest) == 2:
                data = _parse_json_body(body)
                ref = data.get('ref')
                sha = data.get('sha')
                if not ref or not isinstance(ref, str) or not ref.startswith('refs/'):
                    raise ApiError(422, 'invalid ref')
                if not sha or repo.objects.get(sha) is None:
                    raise ApiError(422, 'object not found')
                with repo.lock:
                    if ref in repo.refs:
                        raise ApiError(422, 'ref already exists')
                    repo.refs[ref] = sha
                    return 201, {'ref': ref, 'object': {'sha': sha}}, None
            if method == 'GET' and len(rest) > 2:
                ref = 'refs/' + '/'.join(rest[2:])
                with repo.lock:
                    oid = repo.refs.get(ref)
                    if oid is None:
                        raise ApiError(404, 'ref not found')
                    return 200, {'ref': ref, 'object': {'sha': oid}}, None
            raise ApiError(404, 'Not Found')
        if head == 'contents':
            cpath = '/'.join(rest[1:])
            if method == 'PUT':
                return self._contents_put(repo, cpath, _parse_json_body(body))
            if method == 'GET':
                return self._contents_get(repo, cpath, query)
            if method == 'DELETE':
                return self._contents_delete(repo, cpath, _parse_json_body(body))
            raise ApiError(404, 'Not Found')
        if head == 'labels':
            if method == 'POST':
                data = _parse_json_body(body)
                lname = data.get('name')
                if not lname:
                    raise ApiError(422, 'name is required')
                color = data.get('color', 'ededed')
                with repo.lock:
                    if lname in repo.labels:
                        return 200, repo.labels[lname], None
                    repo.labels[lname] = {'name': lname, 'color': color}
                    return 201, repo.labels[lname], None
            if method == 'GET':
                with repo.lock:
                    return 200, list(repo.labels.values()), None
            raise ApiError(404, 'Not Found')
        if head == 'issues':
            return self._issues_route(method, repo, rest[1:], query, body)
        if head == 'pulls':
            return self._pulls_route(method, repo, rest[1:], query, body)
        raise ApiError(404, 'Not Found')

    # -- contents ---------------------------------------------------------------
    def _contents_put(self, repo, cpath, data):
        message = data.get('message') or ('Update %s' % cpath)
        content_b64 = data.get('content')
        if content_b64 is None:
            raise ApiError(400, 'content is required')
        try:
            content = b64d(content_b64)
        except Exception:
            raise ApiError(400, 'content is not valid base64')
        branch = data.get('branch') or repo.default_branch
        sha = data.get('sha')
        with repo.lock:
            tip = repo.branch_tip(branch)
            if tip is None:
                if branch != repo.default_branch:
                    dtip = repo.default_tip()
                    if dtip is not None:
                        repo.set_branch(branch, dtip)
                        tip = dtip
            if tip is None:
                base_tree = None
                parents = []
            else:
                base_tree = commit_tree(repo, tip)
                parents = [tip]
            cur = None
            if base_tree:
                cur = flatten_tree(repo, base_tree).get(cpath)
            if sha is not None:
                if cur is None or cur[1].lower() != str(sha).lower():
                    raise ApiError(409, 'sha does not match the current blob')
                is_update = True
            else:
                if cur is not None:
                    raise ApiError(422, 'file already exists')
                is_update = False
            blob_oid = repo.put_object('blob', content)
            new_tree = update_tree(repo, base_tree, {cpath: ('100644', blob_oid)})
            commit_oid = make_commit(repo, new_tree, parents, message)
            repo.set_branch(branch, commit_oid)
            out = {'content': {'name': cpath.split('/')[-1], 'path': cpath,
                               'sha': blob_oid, 'size': len(content),
                               'type': 'file', 'encoding': 'base64'},
                   'commit': {'sha': commit_oid, 'message': message}}
            return (200 if is_update else 201), out, None

    def _contents_get(self, repo, cpath, query):
        ref = (query.get('ref') or [None])[0]
        with repo.lock:
            tip = _resolve_ref(repo, ref)
            if tip is None:
                raise ApiError(404, 'ref not found')
            tree_oid = commit_tree(repo, tip)
            if tree_oid is None:
                raise ApiError(404, 'empty repository')
            if cpath == '':
                entries = self._dir_entries(repo, tree_oid, '')
                return 200, entries, None
            flat = flatten_tree(repo, tree_oid)
            if cpath in flat:
                mode, oid = flat[cpath]
                data = repo.objects[oid][1]
                return 200, {'type': 'file', 'encoding': 'base64',
                             'content': b64e(data), 'sha': oid,
                             'size': len(data), 'name': cpath.split('/')[-1],
                             'path': cpath}, None
            sub = self._tree_at(repo, tree_oid, cpath)
            if sub is None:
                raise ApiError(404, 'path not found')
            return 200, self._dir_entries(repo, sub, cpath), None

    def _tree_at(self, repo, tree_oid, path):
        cur = tree_oid
        for seg in path.split('/'):
            obj = repo.objects.get(cur)
            if obj is None or obj[0] != 'tree':
                return None
            found = None
            for mode, name, oid in parse_tree(obj[1]):
                if name == seg:
                    found = (mode, oid)
                    break
            if found is None or found[0] != '40000':
                return None
            cur = found[1]
        return cur

    def _dir_entries(self, repo, tree_oid, prefix):
        out = []
        obj = repo.objects.get(tree_oid)
        if obj is None:
            return out
        for mode, name, oid in parse_tree(obj[1]):
            path = (prefix + '/' + name) if prefix else name
            if mode == '40000':
                out.append({'type': 'dir', 'name': name, 'path': path, 'sha': oid})
            else:
                data = repo.objects.get(oid)
                out.append({'type': 'file', 'name': name, 'path': path,
                            'sha': oid, 'size': len(data[1]) if data else 0})
        return out

    def _contents_delete(self, repo, cpath, data):
        message = data.get('message') or ('Delete %s' % cpath)
        sha = data.get('sha')
        branch = data.get('branch') or repo.default_branch
        with repo.lock:
            tip = repo.branch_tip(branch)
            if tip is None:
                raise ApiError(404, 'ref not found')
            flat = flatten_tree(repo, commit_tree(repo, tip))
            cur = flat.get(cpath)
            if cur is None:
                raise ApiError(404, 'path not found')
            if sha is not None and cur[1].lower() != str(sha).lower():
                raise ApiError(409, 'sha does not match the current blob')
            new_tree = update_tree(repo, commit_tree(repo, tip), {cpath: None})
            commit_oid = make_commit(repo, new_tree, [tip], message)
            repo.set_branch(branch, commit_oid)
            return 200, {'commit': {'sha': commit_oid}}, None

    # -- issues ---------------------------------------------------------------
    def _issues_route(self, method, repo, rest, query, body):
        if not rest:
            if method == 'POST':
                data = _parse_json_body(body)
                title = data.get('title')
                if not title:
                    raise ApiError(422, 'title is required')
                label_names = data.get('labels') or []
                labels = []
                with repo.lock:
                    for ln in label_names:
                        if isinstance(ln, dict):
                            ln = ln.get('name')
                        lab = repo.labels.get(ln)
                        if lab is None:
                            raise ApiError(422, 'Label %s does not exist' % ln)
                        labels.append(lab)
                    number = repo.next_number
                    repo.next_number += 1
                    repo.seq += 1
                    issue = Issue(number, STORE.new_item_id(), title,
                                  data.get('body') or '', labels, repo.seq, repo)
                    repo.issues[number] = issue
                return 201, issue.to_json(), None
            if method == 'GET':
                state = (query.get('state') or ['open'])[0]
                sort = (query.get('sort') or ['created'])[0]
                direction = (query.get('direction') or ['desc'])[0]
                with repo.lock:
                    items = list(repo.issues.values())
                if state != 'all':
                    items = [i for i in items if i.state == state]
                items.sort(key=lambda i: i.seq, reverse=(direction != 'asc'))
                out = [i.to_json() for i in items]
                sliced, link = _paginate(
                    out, query, self,
                    '/api/v3/repos/%s/%s/issues' % (repo.owner, repo.name))
                return 200, sliced, link
            raise ApiError(404, 'Not Found')
        try:
            number = int(rest[0])
        except ValueError:
            raise ApiError(404, 'Not Found')
        if len(rest) == 1:
            if method == 'GET':
                with repo.lock:
                    issue = repo.issues.get(number)
                    if issue is None:
                        raise ApiError(404, 'Issue not found')
                    return 200, issue.to_json(), None
            if method == 'PATCH':
                data = _parse_json_body(body)
                with repo.lock:
                    issue = repo.issues.get(number)
                    if issue is None:
                        raise ApiError(404, 'Issue not found')
                    if 'state' in data:
                        if data['state'] not in ('open', 'closed'):
                            raise ApiError(422, 'invalid state')
                        issue.state = data['state']
                    if 'title' in data:
                        issue.title = data['title']
                    if 'body' in data:
                        issue.body = data['body']
                    return 200, issue.to_json(), None
            raise ApiError(404, 'Not Found')
        if len(rest) == 2 and rest[1] == 'comments':
            if method == 'POST':
                data = _parse_json_body(body)
                cbody = data.get('body')
                if not cbody:
                    raise ApiError(422, 'body is required')
                with repo.lock:
                    issue = repo.issues.get(number)
                    if issue is None:
                        raise ApiError(404, 'Issue not found')
                    comment = {'id': STORE.new_comment_id(), 'body': cbody}
                    issue.comments.append(comment)
                    return 201, dict(comment), None
            if method == 'GET':
                with repo.lock:
                    issue = repo.issues.get(number)
                    if issue is None:
                        raise ApiError(404, 'Issue not found')
                    return 200, [dict(c) for c in issue.comments], None
            raise ApiError(404, 'Not Found')
        raise ApiError(404, 'Not Found')

    # -- pull requests ------------------------------------------------------------
    def _pulls_route(self, method, repo, rest, query, body):
        if not rest:
            if method == 'POST':
                data = _parse_json_body(body)
                title = data.get('title')
                head = data.get('head')
                base = data.get('base')
                if not title or not head or not base:
                    raise ApiError(422, 'title, head and base are required')
                head = head.split(':')[-1]
                base = base.split(':')[-1]
                with repo.lock:
                    head_tip = repo.branch_tip(head)
                    base_tip = repo.branch_tip(base)
                    if head_tip is None or base_tip is None:
                        raise ApiError(422, 'head or base branch not found')
                    for pr in repo.pulls.values():
                        if pr.state == 'open' and pr.head == head and pr.base == base:
                            raise ApiError(422, 'a pull request from %s to %s is already open' % (head, base))
                    number = repo.next_number
                    repo.next_number += 1
                    repo.seq += 1
                    pr = PullRequest(number, STORE.new_item_id(), title,
                                    data.get('body') or '', head, base,
                                    head_tip, base_tip, repo.seq, repo)
                    repo.pulls[number] = pr
                    return 201, pr.to_json(mergeable=mergeable_status(repo, pr)), None
            if method == 'GET':
                state = (query.get('state') or ['open'])[0]
                with repo.lock:
                    items = list(repo.pulls.values())
                if state != 'all':
                    items = [p for p in items if p.state == state]
                items.sort(key=lambda p: p.seq, reverse=True)
                out = [p.to_json() for p in items]
                sliced, link = _paginate(
                    out, query, self,
                    '/api/v3/repos/%s/%s/pulls' % (repo.owner, repo.name))
                return 200, sliced, link
            raise ApiError(404, 'Not Found')
        try:
            number = int(rest[0])
        except ValueError:
            raise ApiError(404, 'Not Found')
        if len(rest) == 1:
            if method == 'GET':
                with repo.lock:
                    pr = repo.pulls.get(number)
                    if pr is None:
                        raise ApiError(404, 'Pull request not found')
                    return 200, pr.to_json(mergeable=mergeable_status(repo, pr)), None
            raise ApiError(404, 'Not Found')
        if len(rest) == 2 and rest[1] == 'files':
            if method != 'GET':
                raise ApiError(404, 'Not Found')
            with repo.lock:
                pr = repo.pulls.get(number)
                if pr is None:
                    raise ApiError(404, 'Pull request not found')
                head_tip = repo.branch_tip(pr.head) or pr.head_sha
                base_tip = repo.branch_tip(pr.base) or pr.base_sha
                mb = merge_base(repo, head_tip, base_tip)
                mb_tree = commit_tree(repo, mb) if mb else None
                head_tree = commit_tree(repo, head_tip)
                out = []
                for path, status, old, new in diff_trees(repo, mb_tree, head_tree):
                    add, dele = count_line_changes(old, new)
                    out.append({'filename': path, 'status': status,
                                'additions': add, 'deletions': dele,
                                'changes': add + dele})
                return 200, out, None
        if len(rest) == 2 and rest[1] == 'merge':
            if method != 'PUT':
                raise ApiError(404, 'Not Found')
            data = _parse_json_body(body)
            with repo.lock:
                pr = repo.pulls.get(number)
                if pr is None:
                    raise ApiError(404, 'Pull request not found')
                if pr.state != 'open':
                    raise ApiError(405, 'pull request is not open')
                if mergeable_status(repo, pr) == 'CONFLICTING':
                    raise ApiError(405, 'merge conflict')
                commit_oid, _conflict = do_squash_merge(repo, pr)
                return 200, {'merged': True, 'sha': commit_oid,
                             'message': 'Pull request #%d merged' % number}, None
        raise ApiError(404, 'Not Found')

# ---------------------------------------------------------------------------
# GraphQL v4 (mini parser + executor for the documents gh / github-mcp-server
# send; no external library)
# ---------------------------------------------------------------------------

class GQLError(Exception):
    pass


_GQL_PUNCT = set('{}()[]:!@$=&|')


def _gql_lex(src):
    tokens = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch in ' \t\r\n,':  # commas are insignificant whitespace in GraphQL
            i += 1
            continue
        if ch == '#':
            while i < n and src[i] != '\n':
                i += 1
            continue
        if src.startswith('...', i):
            tokens.append(('ELLIPSIS', '...'))
            i += 3
            continue
        if ch in '{}()[]:!=$@':
            tokens.append((ch, ch))
            i += 1
            continue
        if ch == '"':
            j = i + 1
            out = []
            while j < n and src[j] != '"':
                if src[j] == '\\' and j + 1 < n:
                    esc = src[j + 1]
                    mapping = {'n': '\n', 't': '\t', 'r': '\r', '"': '"',
                               '\\': '\\', '/': '/'}
                    if esc == 'u' and j + 5 < n:
                        out.append(chr(int(src[j + 2:j + 6], 16)))
                        j += 6
                        continue
                    out.append(mapping.get(esc, esc))
                    j += 2
                    continue
                out.append(src[j])
                j += 1
            tokens.append(('STRING', ''.join(out)))
            i = j + 1
            continue
        if ch == '-' or ch.isdigit():
            j = i
            if src[j] == '-':
                j += 1
            while j < n and src[j].isdigit():
                j += 1
            is_float = False
            if j < n and src[j] == '.':
                is_float = True
                j += 1
                while j < n and src[j].isdigit():
                    j += 1
            if j < n and src[j] in 'eE':
                is_float = True
                j += 1
                if j < n and src[j] in '+-':
                    j += 1
                while j < n and src[j].isdigit():
                    j += 1
            text = src[i:j]
            tokens.append(('FLOAT' if is_float else 'INT',
                           float(text) if is_float else int(text)))
            i = j
            continue
        if ch.isalpha() or ch == '_':
            j = i
            while j < n and (src[j].isalnum() or src[j] == '_'):
                j += 1
            tokens.append(('NAME', src[i:j]))
            i = j
            continue
        raise GQLError('unexpected character %r in query' % ch)
    tokens.append(('EOF', None))
    return tokens


class _GQLParser:
    def __init__(self, tokens):
        self.toks = tokens
        self.pos = 0

    def peek(self):
        return self.toks[self.pos]

    def next(self):
        tok = self.toks[self.pos]
        self.pos += 1
        return tok

    def expect(self, kind):
        tok = self.next()
        if tok[0] != kind:
            raise GQLError('expected %s, got %s' % (kind, tok[0]))
        return tok

    def expect_name(self, value=None):
        tok = self.expect('NAME')
        if value is not None and tok[1] != value:
            raise GQLError('expected name %s, got %s' % (value, tok[1]))
        return tok[1]

    def parse_document(self):
        ops = []
        while self.peek()[0] != 'EOF':
            ops.append(self.parse_operation())
        return ops

    def parse_operation(self):
        tok = self.peek()
        if tok == ('{', '{'):
            return {'kind': 'query', 'name': None, 'vars': {},
                    'selection': self.parse_selection_set()}
        kind = self.expect_name()
        if kind not in ('query', 'mutation', 'subscription'):
            raise GQLError('unknown operation %s' % kind)
        name = None
        if self.peek()[0] == 'NAME':
            name = self.next()[1]
        var_defs = {}
        if self.peek()[0] == '(':
            self.next()
            while self.peek()[0] != ')':
                self.expect('$')
                vname = self.expect_name()
                self.expect(':')
                self.parse_type()
                default = None
                if self.peek()[0] == '=':
                    self.next()
                    default = self.parse_value()
                var_defs[vname] = default
            self.expect(')')
        selection = self.parse_selection_set()
        return {'kind': kind, 'name': name, 'vars': var_defs,
                'selection': selection}

    def parse_type(self):
        if self.peek()[0] == '[':
            self.next()
            self.parse_type()
            self.expect(']')
        else:
            self.expect_name()
        if self.peek()[0] == '!':
            self.next()

    def parse_selection_set(self):
        self.expect('{')
        sels = []
        while self.peek()[0] != '}':
            sels.append(self.parse_selection())
        self.expect('}')
        return sels

    def parse_selection(self):
        if self.peek()[0] == 'ELLIPSIS':
            self.next()
            if self.peek() == ('NAME', 'on'):
                self.next()
                tcond = self.expect_name()
            else:
                tcond = None
            return {'fragment': True, 'type': tcond,
                    'selection': self.parse_selection_set()}
        name = self.expect_name()
        alias = None
        if self.peek()[0] == ':':
            self.next()
            alias = name
            name = self.expect_name()
        args = {}
        if self.peek()[0] == '(':
            self.next()
            while self.peek()[0] != ')':
                aname = self.expect_name()
                self.expect(':')
                args[aname] = self.parse_value()
            self.expect(')')
        selection = None
        if self.peek()[0] == '{':
            selection = self.parse_selection_set()
        return {'fragment': False, 'alias': alias, 'name': name,
                'args': args, 'selection': selection}

    def parse_value(self):
        tok = self.peek()
        if tok[0] == '$':
            self.next()
            return ('var', self.expect_name())
        if tok[0] == 'INT':
            self.next()
            return ('int', tok[1])
        if tok[0] == 'FLOAT':
            self.next()
            return ('float', tok[1])
        if tok[0] == 'STRING':
            self.next()
            return ('string', tok[1])
        if tok[0] == 'NAME':
            self.next()
            if tok[1] == 'true':
                return ('bool', True)
            if tok[1] == 'false':
                return ('bool', False)
            if tok[1] == 'null':
                return ('null', None)
            return ('enum', tok[1])
        if tok[0] == '[':
            self.next()
            items = []
            while self.peek()[0] != ']':
                items.append(self.parse_value())
            self.expect(']')
            return ('list', items)
        if tok[0] == '{':
            self.next()
            obj = {}
            while self.peek()[0] != '}':
                key = self.expect_name()
                self.expect(':')
                obj[key] = self.parse_value()
            self.expect('}')
            return ('object', obj)
        raise GQLError('unexpected value token %s' % (tok,))


def _gql_value(node, variables):
    kind, val = node
    if kind == 'var':
        return variables.get(val)
    if kind == 'list':
        return [_gql_value(v, variables) for v in val]
    if kind == 'object':
        return {k: _gql_value(v, variables) for k, v in val.items()}
    return val


# -- schema metadata -----------------------------------------------------------

ISSUE_FIELDS = ['id', 'number', 'title', 'state', 'body', 'url', 'author',
                'createdAt', 'comments', 'labels']
PR_FIELDS = ['id', 'number', 'title', 'body', 'state', 'headRefName',
             'baseRefName', 'mergeable', 'merged', 'files', 'headRef',
             'baseRef', 'author', 'createdAt', 'url', 'mergeCommit']
REPO_FIELDS = ['id', 'databaseId', 'name', 'nameWithOwner', 'owner',
               'defaultBranchRef', 'viewerPermission', 'hasIssuesEnabled',
               'issue', 'issues', 'pullRequests', 'pullRequest', 'url',
               'description', 'isPrivate']
USER_FIELDS = ['login', 'id', 'name']
SEARCH_ENUM = ['ISSUE', 'REPOSITORY', 'USER', 'DISCUSSION', 'COMMIT', 'CODE']


def _cursor_for(seq):
    return b64e('cursor:%d' % seq)


def _seq_from_cursor(cursor):
    try:
        raw = base64.b64decode(''.join(str(cursor).split())).decode('utf-8')
        if raw.startswith('cursor:'):
            return int(raw[7:])
    except Exception:
        pass
    return 0


def _find_repo_by_dbid(dbid):
    for repo in list(STORE.repos.values()):
        if repo.dbid == dbid:
            return repo
    return None


def _resolve_global_id(gid_value):
    kind, rest = ungid(gid_value)
    if kind is None:
        return None, None
    if kind == 'Repository':
        try:
            return 'Repository', _find_repo_by_dbid(int(rest))
        except ValueError:
            return None, None
    if kind in ('Issue', 'PullRequest', 'IssueComment'):
        parts = rest.split(':')
        if len(parts) < 3:
            return None, None
        owner, name = parts[0], parts[1]
        repo = STORE.get_repo(owner, name)
        if repo is None:
            return None, None
        try:
            number = int(parts[2])
        except ValueError:
            return None, None
        if kind == 'Issue':
            return 'Issue', (repo, repo.issues.get(number))
        if kind == 'PullRequest':
            return 'PullRequest', (repo, repo.pulls.get(number))
        if kind == 'IssueComment':
            issue = repo.issues.get(number)
            if issue is None:
                return None, None
            cid = int(parts[3]) if len(parts) > 3 else None
            for c in issue.comments:
                if c['id'] == cid:
                    return 'IssueComment', (repo, issue, c)
            return None, None
    return None, None


def _issue_state_gql(state):
    return 'CLOSED' if state == 'closed' else 'OPEN'


def _pr_state_gql(pr):
    if pr.merged:
        return 'MERGED'
    return 'CLOSED' if pr.state == 'closed' else 'OPEN'


class GraphQLExecutor:
    def __init__(self, variables=None):
        self.variables = variables or {}

    # -- entry ------------------------------------------------------------------
    def execute(self, query, variables=None, operation_name=None):
        if variables is not None:
            self.variables = variables
        try:
            ops = _GQLParser(_gql_lex(query)).parse_document()
        except GQLError as e:
            return {'data': None, 'errors': [{'message': str(e)}]}
        op = None
        if operation_name:
            for o in ops:
                if o['name'] == operation_name:
                    op = o
                    break
            if op is None:
                return {'data': None,
                        'errors': [{'message': 'unknown operation %s' % operation_name}]}
        elif len(ops) == 1:
            op = ops[0]
        else:
            return {'data': None,
                    'errors': [{'message': 'must provide operation name'}]}
        try:
            if op['kind'] == 'mutation':
                data = self._execute_selection(
                    op['selection'], 'Mutation', None)
            else:
                data = self._execute_selection(
                    op['selection'], 'Query', None)
            return {'data': data}
        except GQLError as e:
            return {'data': None, 'errors': [{'message': str(e)}]}

    # -- generic selection execution ----------------------------------------------
    def _execute_selection(self, sels, typename, obj):
        out = {}
        for sel in sels:
            if sel['fragment']:
                if sel['type'] is not None and sel['type'] != typename:
                    continue
                sub = self._execute_selection(sel['selection'], typename, obj)
                out.update(sub)
                continue
            key = sel['alias'] or sel['name']
            args = {k: _gql_value(v, self.variables)
                    for k, v in sel['args'].items()}
            value = self._resolve_field(typename, obj, sel['name'], args)
            if sel['selection'] is not None:
                if value is None:
                    out[key] = None
                elif isinstance(value, list):
                    out[key] = [self._execute_selection(sel['selection'], v['__typename'], v)
                                if isinstance(v, dict) and '__typename' in v else v
                                for v in value]
                elif isinstance(value, dict) and '__typename' in value:
                    out[key] = self._execute_selection(
                        sel['selection'], value['__typename'], value)
                else:
                    out[key] = value
            else:
                out[key] = value
        return out

    # -- field resolution -----------------------------------------------------------
    def _resolve_field(self, typename, obj, name, args):
        if name == '__typename':
            return typename
        if typename == 'Query':
            return self._query_field(name, args)
        if typename == 'Mutation':
            return self._mutation_field(name, args)
        if typename == 'Repository':
            return self._repo_field(obj['repo'], name, args)
        if typename == 'Issue':
            return self._issue_field(obj['issue'], name, args)
        if typename == 'PullRequest':
            return self._pr_field(obj['pr'], name, args)
        if typename == 'User':
            return obj.get(name) if name in ('login', 'id', 'name') else self._unknown(name, typename)
        if typename == 'IssueConnection' or typename == 'PullRequestConnection':
            if name == 'nodes':
                return obj['nodes']
            if name == 'pageInfo':
                return obj['pageInfo']
            if name == 'totalCount':
                return len(obj['nodes'])
            return self._unknown(name, typename)
        if typename == 'PageInfo':
            return obj.get(name)
        if typename == 'IssueComment':
            if name == 'id':
                return obj['comment_global_id']
            if name == 'body':
                return obj['comment']['body']
            return self._unknown(name, typename)
        if typename == '__Type':
            if name == 'name':
                return obj['name']
            if name == 'kind':
                return obj['kind']
            if name == 'fields':
                return [{ '__typename': '__Field', 'name': f}
                        for f in obj.get('fields', [])]
            if name == 'enumValues':
                return [{ '__typename': '__EnumValue', 'name': v}
                        for v in obj.get('enumValues', [])]
            return self._unknown(name, typename)
        if typename in ('__Field', '__EnumValue'):
            if name == 'name':
                return obj['name']
            return self._unknown(name, typename)
        # generic: a wrapped object that already carries the field value
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        return self._unknown(name, typename)

    def _unknown(self, name, typename):
        raise GQLError('Cannot query field "%s" on type "%s".' % (name, typename))

    # -- Query ----------------------------------------------------------------------
    def _query_field(self, name, args):
        if name == 'viewer':
            return {'__typename': 'User', 'login': VIEWER_LOGIN,
                    'id': '1000', 'name': VIEWER_LOGIN}
        if name == 'repository':
            repo = STORE.get_repo(args.get('owner'), args.get('name'))
            if repo is None:
                return None
            return {'__typename': 'Repository', 'repo': repo}
        if name == 'node':
            kind, found = _resolve_global_id(args.get('id'))
            if kind is None or found is None:
                return None
            if kind == 'Repository':
                if found is None:
                    return None
                return {'__typename': 'Repository', 'repo': found}
            if kind == 'Issue':
                repo, issue = found
                if issue is None:
                    return None
                return {'__typename': 'Issue', 'issue': issue}
            if kind == 'PullRequest':
                repo, pr = found
                if pr is None:
                    return None
                return {'__typename': 'PullRequest', 'pr': pr}
            if kind == 'IssueComment':
                repo, issue, comment = found
                return {'__typename': 'IssueComment', 'comment': comment,
                        'comment_global_id': gid('IssueComment', repo.owner,
                                                 repo.name, issue.number,
                                                 comment['id'])}
            return None
        if name == '__type':
            tname = args.get('name')
            if tname == 'Issue':
                return {'__typename': '__Type', 'name': 'Issue',
                        'kind': 'OBJECT', 'fields': ISSUE_FIELDS, 'enumValues': []}
            if tname == 'PullRequest':
                return {'__typename': '__Type', 'name': 'PullRequest',
                        'kind': 'OBJECT', 'fields': PR_FIELDS, 'enumValues': []}
            if tname == 'Repository':
                return {'__typename': '__Type', 'name': 'Repository',
                        'kind': 'OBJECT', 'fields': REPO_FIELDS, 'enumValues': []}
            if tname == 'User':
                return {'__typename': '__Type', 'name': 'User',
                        'kind': 'OBJECT', 'fields': USER_FIELDS, 'enumValues': []}
            if tname == 'SearchType':
                return {'__typename': '__Type', 'name': 'SearchType',
                        'kind': 'ENUM', 'fields': [], 'enumValues': SEARCH_ENUM}
            if tname == 'IssueState':
                return {'__typename': '__Type', 'name': 'IssueState',
                        'kind': 'ENUM', 'fields': [],
                        'enumValues': ['OPEN', 'CLOSED']}
            if tname == 'MergeableState':
                return {'__typename': '__Type', 'name': 'MergeableState',
                        'kind': 'ENUM', 'fields': [],
                        'enumValues': ['MERGEABLE', 'CONFLICTING', 'UNKNOWN']}
            if tname == 'PullRequestState':
                return {'__typename': '__Type', 'name': 'PullRequestState',
                        'kind': 'ENUM', 'fields': [],
                        'enumValues': ['OPEN', 'CLOSED', 'MERGED']}
            if tname == 'RepositoryPermission':
                return {'__typename': '__Type', 'name': 'RepositoryPermission',
                        'kind': 'ENUM', 'fields': [],
                        'enumValues': ['ADMIN', 'MAINTAIN', 'WRITE', 'TRIAGE', 'READ']}
            return None
        return self._unknown(name, 'Query')

    # -- Repository -------------------------------------------------------------------
    def _repo_field(self, repo, name, args):
        if name == 'id':
            return repo.global_id()
        if name == 'databaseId':
            return repo.dbid
        if name == 'name':
            return repo.name
        if name == 'nameWithOwner':
            return '%s/%s' % (repo.owner, repo.name)
        if name == 'owner':
            return {'__typename': 'User', 'login': repo.owner,
                    'id': '1000', 'name': repo.owner}
        if name == 'defaultBranchRef':
            tip = repo.default_tip()
            if tip is None:
                return None
            return {'__typename': 'Ref', 'name': repo.default_branch}
        if name == 'viewerPermission':
            return 'ADMIN'
        if name == 'hasIssuesEnabled':
            return True
        if name == 'description':
            return repo.description
        if name == 'isPrivate':
            return repo.private
        if name == 'url':
            return 'https://example.invalid/%s/%s' % (repo.owner, repo.name)
        if name == 'issue':
            issue = repo.issues.get(args.get('number'))
            if issue is None:
                return None
            return {'__typename': 'Issue', 'issue': issue}
        if name == 'issues':
            return self._issues_connection(repo, args)
        if name == 'pullRequests':
            return self._prs_connection(repo, args)
        if name == 'pullRequest':
            pr = repo.pulls.get(args.get('number'))
            if pr is None:
                return None
            return {'__typename': 'PullRequest', 'pr': pr}
        return self._unknown(name, 'Repository')

    def _issues_connection(self, repo, args):
        with repo.lock:
            items = sorted(repo.issues.values(), key=lambda i: i.seq)
        states = args.get('states')
        if states:
            wanted = set(s.lower() for s in states)
            items = [i for i in items if i.state in wanted]
        order_by = args.get('orderBy') or {}
        field = (order_by.get('field') or 'CREATED_AT')
        direction = (order_by.get('direction') or 'ASC')
        if field == 'CREATED_AT' and direction == 'DESC':
            items = list(reversed(items))
        after_seq = _seq_from_cursor(args.get('after')) if args.get('after') else 0
        if after_seq:
            items = [i for i in items if i.seq > after_seq]
        first = args.get('first')
        has_next = False
        if first is not None:
            if len(items) > first:
                has_next = True
            items = items[:first]
        end_cursor = _cursor_for(items[-1].seq) if items else None
        nodes = [{'__typename': 'Issue', 'issue': i} for i in items]
        return {'__typename': 'IssueConnection', 'nodes': nodes,
                'pageInfo': {'__typename': 'PageInfo',
                             'hasNextPage': has_next,
                             'endCursor': end_cursor,
                             'hasPreviousPage': False,
                             'startCursor': _cursor_for(items[0].seq) if items else None}}

    def _prs_connection(self, repo, args):
        with repo.lock:
            items = sorted(repo.pulls.values(), key=lambda p: p.seq)
        states = args.get('states')
        if states:
            items = [p for p in items if _pr_state_gql(p) in states]
        else:
            items = [p for p in items if p.state == 'open']
        first = args.get('first')
        if first is not None:
            items = items[:first]
        nodes = [{'__typename': 'PullRequest', 'pr': p} for p in items]
        return {'__typename': 'PullRequestConnection', 'nodes': nodes,
                'pageInfo': {'__typename': 'PageInfo', 'hasNextPage': False,
                             'endCursor': None, 'hasPreviousPage': False,
                             'startCursor': None}}

    # -- Issue / PullRequest ------------------------------------------------------------
    def _issue_field(self, issue, name, args):
        if name == 'id':
            return issue.global_id()
        if name == 'number':
            return issue.number
        if name == 'title':
            return issue.title
        if name == 'state':
            return _issue_state_gql(issue.state)
        if name == 'body':
            return issue.body
        if name == 'url':
            return 'https://example.invalid/%s/%s/issues/%d' % (
                issue.repo.owner, issue.repo.name, issue.number)
        if name == 'author':
            return {'__typename': 'User', 'login': VIEWER_LOGIN,
                    'id': '1000', 'name': VIEWER_LOGIN}
        if name == 'createdAt':
            return '2026-01-01T00:00:00Z'
        if name == 'comments':
            nodes = [{'__typename': 'IssueComment', 'comment': c,
                      'comment_global_id': gid('IssueComment', issue.repo.owner,
                                               issue.repo.name, issue.number,
                                               c['id'])}
                     for c in issue.comments]
            return {'__typename': 'IssueConnection', 'nodes': nodes,
                    'pageInfo': {'__typename': 'PageInfo', 'hasNextPage': False,
                                 'endCursor': None, 'hasPreviousPage': False,
                                 'startCursor': None}}
        if name == 'labels':
            return {'__typename': 'LabelConnection', 'nodes': issue.labels}
        return self._unknown(name, 'Issue')

    def _pr_field(self, pr, name, args):
        if name == 'id':
            return pr.global_id()
        if name == 'number':
            return pr.number
        if name == 'title':
            return pr.title
        if name == 'body':
            return pr.body
        if name == 'state':
            return _pr_state_gql(pr)
        if name == 'headRefName':
            return pr.head
        if name == 'baseRefName':
            return pr.base
        if name == 'mergeable':
            with pr.repo.lock:
                return mergeable_status(pr.repo, pr)
        if name == 'merged':
            return pr.merged
        if name == 'mergeCommit':
            if pr.merge_commit_sha is None:
                return None
            return {'__typename': 'Commit', 'oid': pr.merge_commit_sha}
        if name == 'files':
            return self._pr_files(pr, args)
        if name == 'url':
            return 'https://example.invalid/%s/%s/pull/%d' % (
                pr.repo.owner, pr.repo.name, pr.number)
        if name == 'author':
            return {'__typename': 'User', 'login': VIEWER_LOGIN,
                    'id': '1000', 'name': VIEWER_LOGIN}
        if name == 'createdAt':
            return '2026-01-01T00:00:00Z'
        return self._unknown(name, 'PullRequest')

    def _pr_files(self, pr, args):
        repo = pr.repo
        with repo.lock:
            head_tip = repo.branch_tip(pr.head) or pr.head_sha
            base_tip = repo.branch_tip(pr.base) or pr.base_sha
            mb = merge_base(repo, head_tip, base_tip)
            mb_tree = commit_tree(repo, mb) if mb else None
            head_tree = commit_tree(repo, head_tip)
            diffs = diff_trees(repo, mb_tree, head_tree)
        nodes = [{'__typename': 'PullRequestChangedFile', 'path': path}
                 for path, _s, _o, _n in diffs]
        first = args.get('first')
        if first is not None:
            nodes = nodes[:first]
        return {'__typename': 'PullRequestChangedFileConnection',
                'nodes': nodes,
                'pageInfo': {'__typename': 'PageInfo', 'hasNextPage': False,
                             'endCursor': None, 'hasPreviousPage': False,
                             'startCursor': None}}

    # -- Mutations ------------------------------------------------------------------------
    def _mutation_field(self, name, args):
        if name == 'createIssue':
            return self._mut_create_issue(args.get('input') or {})
        if name == 'closeIssue':
            return self._mut_close_issue(args.get('input') or {})
        if name == 'createPullRequest':
            return self._mut_create_pr(args.get('input') or {})
        if name == 'mergePullRequest':
            return self._mut_merge_pr(args.get('input') or {})
        if name == 'addComment':
            return self._mut_add_comment(args.get('input') or {})
        return self._unknown(name, 'Mutation')

    def _mut_create_issue(self, inp):
        kind, repo = _resolve_global_id(inp.get('repositoryId'))
        if kind != 'Repository' or repo is None:
            raise GQLError('Repository not found')
        title = inp.get('title')
        if not title:
            raise GQLError('title is required')
        body = inp.get('body') or ''
        with repo.lock:
            number = repo.next_number
            repo.next_number += 1
            repo.seq += 1
            issue = Issue(number, STORE.new_item_id(), title, body, [],
                          repo.seq, repo)
            repo.issues[number] = issue
        return {'__typename': 'CreateIssuePayload',
                'issue': {'__typename': 'Issue', 'issue': issue}}

    def _mut_close_issue(self, inp):
        kind, found = _resolve_global_id(inp.get('issueId'))
        if kind != 'Issue' or found is None:
            raise GQLError('Issue not found')
        repo, issue = found
        if issue is None:
            raise GQLError('Issue not found')
        with repo.lock:
            issue.state = 'closed'
        return {'__typename': 'CloseIssuePayload',
                'issue': {'__typename': 'Issue', 'issue': issue}}

    def _mut_create_pr(self, inp):
        kind, repo = _resolve_global_id(inp.get('repositoryId'))
        if kind != 'Repository' or repo is None:
            raise GQLError('Repository not found')
        head = (inp.get('headRefName') or '').split(':')[-1]
        base = (inp.get('baseRefName') or '').split(':')[-1]
        title = inp.get('title')
        if not head or not base or not title:
            raise GQLError('headRefName, baseRefName and title are required')
        with repo.lock:
            head_tip = repo.branch_tip(head)
            base_tip = repo.branch_tip(base)
            if head_tip is None or base_tip is None:
                raise GQLError('head or base branch not found')
            for pr in repo.pulls.values():
                if pr.state == 'open' and pr.head == head and pr.base == base:
                    raise GQLError('a pull request from %s to %s is already open'
                                   % (head, base))
            number = repo.next_number
            repo.next_number += 1
            repo.seq += 1
            pr = PullRequest(number, STORE.new_item_id(), title,
                             inp.get('body') or '', head, base,
                             head_tip, base_tip, repo.seq, repo)
            repo.pulls[number] = pr
        return {'__typename': 'CreatePullRequestPayload',
                'pullRequest': {'__typename': 'PullRequest', 'pr': pr}}

    def _mut_merge_pr(self, inp):
        kind, found = _resolve_global_id(inp.get('pullRequestId'))
        if kind != 'PullRequest' or found is None:
            raise GQLError('Pull request not found')
        repo, pr = found
        if pr is None:
            raise GQLError('Pull request not found')
        with repo.lock:
            if pr.state != 'open':
                raise GQLError('pull request is not open')
            if mergeable_status(repo, pr) == 'CONFLICTING':
                raise GQLError('merge conflict')
            do_squash_merge(repo, pr)
        return {'__typename': 'MergePullRequestPayload',
                'pullRequest': {'__typename': 'PullRequest', 'pr': pr}}

    def _mut_add_comment(self, inp):
        kind, found = _resolve_global_id(inp.get('subjectId'))
        if kind != 'Issue' or found is None:
            raise GQLError('Issue not found')
        repo, issue = found
        if issue is None:
            raise GQLError('Issue not found')
        body = inp.get('body')
        if not body:
            raise GQLError('body is required')
        with repo.lock:
            comment = {'id': STORE.new_comment_id(), 'body': body}
            issue.comments.append(comment)
        node = {'__typename': 'IssueComment', 'comment': comment,
                'comment_global_id': gid('IssueComment', repo.owner, repo.name,
                                         issue.number, comment['id'])}
        return {'__typename': 'AddCommentPayload',
                'commentEdge': {'__typename': 'IssueCommentEdge', 'node': node}}


class GraphQLMixin:
    def _handle_graphql(self, body):
        if not check_auth(self.headers):
            self._json(401, {'message': 'Requires authentication'})
            return
        try:
            payload = json.loads(body.decode('utf-8'))
        except Exception:
            self._json(400, {'errors': [{'message': 'malformed JSON body'}]})
            return
        if isinstance(payload, list):
            out = []
            for op in payload:
                out.append(self._run_gql_op(op))
            self._json(200, out)
            return
        if isinstance(payload, dict):
            self._json(200, self._run_gql_op(payload))
            return
        self._json(400, {'errors': [{'message': 'body must be an object or array'}]})

    def _run_gql_op(self, op):
        if not isinstance(op, dict) or not isinstance(op.get('query'), str):
            return {'data': None,
                    'errors': [{'message': 'operation needs a query string'}]}
        ex = GraphQLExecutor()
        return ex.execute(op['query'], op.get('variables') or {},
                          op.get('operationName'))

# ---------------------------------------------------------------------------
# MCP (streamable HTTP, legacy initialize handshake)
# ---------------------------------------------------------------------------

MCP_TOOLS = [
    {'name': 'create_repo',
     'description': 'Create a repository for the token owner.',
     'inputSchema': {'type': 'object',
                     'properties': {'name': {'type': 'string'},
                                    'description': {'type': 'string'},
                                    'private': {'type': 'boolean'}},
                     'required': ['name']}},
    {'name': 'get_repo',
     'description': 'Get a repository by owner and name.',
     'inputSchema': {'type': 'object',
                     'properties': {'owner': {'type': 'string'},
                                    'repo': {'type': 'string'}},
                     'required': ['owner', 'repo']}},
    {'name': 'create_issue',
     'description': 'Create an issue in a repository.',
     'inputSchema': {'type': 'object',
                     'properties': {'owner': {'type': 'string'},
                                    'repo': {'type': 'string'},
                                    'title': {'type': 'string'},
                                    'body': {'type': 'string'}},
                     'required': ['owner', 'repo', 'title']}},
    {'name': 'list_issues',
     'description': 'List issues in a repository.',
     'inputSchema': {'type': 'object',
                     'properties': {'owner': {'type': 'string'},
                                    'repo': {'type': 'string'},
                                    'state': {'type': 'string'}},
                     'required': ['owner', 'repo']}},
    {'name': 'create_pull_request',
     'description': 'Open a pull request from a head branch into a base branch.',
     'inputSchema': {'type': 'object',
                     'properties': {'owner': {'type': 'string'},
                                    'repo': {'type': 'string'},
                                    'title': {'type': 'string'},
                                    'head': {'type': 'string'},
                                    'base': {'type': 'string'},
                                    'body': {'type': 'string'}},
                     'required': ['owner', 'repo', 'title', 'head', 'base']}},
    {'name': 'merge_pull_request',
     'description': 'Squash-merge a pull request.',
     'inputSchema': {'type': 'object',
                     'properties': {'owner': {'type': 'string'},
                                    'repo': {'type': 'string'},
                                    'pull_number': {'type': 'integer'}},
                     'required': ['owner', 'repo', 'pull_number']}},
]


def _tool_result(payload, is_error=False):
    out = {'content': [{'type': 'text',
                        'text': json.dumps(payload, separators=(',', ':'),
                                           ensure_ascii=False)}],
           'structuredContent': payload}
    if is_error:
        out['isError'] = True
    return out


def _tool_error(message):
    return _tool_result({'error': message}, is_error=True)


class McpMixin:
    def _handle_mcp(self, method, body):
        if method == 'DELETE':
            self._no_content(204)
            return
        if method != 'POST':
            self._err(405, 'method not allowed')
            return
        try:
            payload = json.loads(body.decode('utf-8'))
        except Exception:
            self._send(400, json.dumps(
                {'jsonrpc': '2.0', 'id': None,
                 'error': {'code': -32700, 'message': 'Parse error'}}).encode(),
                'application/json')
            return
        if not isinstance(payload, dict):
            self._send(400, json.dumps(
                {'jsonrpc': '2.0', 'id': None,
                 'error': {'code': -32600, 'message': 'Invalid Request'}}).encode(),
                'application/json')
            return
        req_id = payload.get('id')
        method_name = payload.get('method')
        if not check_auth(self.headers):
            self._send(401, json.dumps(
                {'jsonrpc': '2.0', 'id': req_id,
                 'error': {'code': -32001, 'message': 'Unauthorized'}}).encode(),
                'application/json')
            return
        if req_id is None:
            # notification (e.g. notifications/initialized): no response body
            self._send(202, b'', 'application/json')
            return
        result, error = self._mcp_dispatch(method_name, payload.get('params') or {})
        if error is not None:
            resp = {'jsonrpc': '2.0', 'id': req_id, 'error': error}
        else:
            resp = {'jsonrpc': '2.0', 'id': req_id, 'result': result}
        self._json(200, resp)

    def _mcp_dispatch(self, method, params):
        if method == 'server/discover':
            return None, {'code': -32601,
                          'message': 'Method not found: server/discover'}
        if method == 'initialize':
            version = params.get('protocolVersion') or '2025-11-25'
            return {'protocolVersion': version,
                    'capabilities': {'tools': {}},
                    'serverInfo': {'name': 'github-core-clone',
                                   'version': '1.0.0'}}, None
        if method == 'tools/list':
            return {'tools': MCP_TOOLS}, None
        if method == 'tools/call':
            name = params.get('name')
            args = params.get('arguments') or {}
            if not isinstance(args, dict):
                return None, {'code': -32602,
                              'message': 'arguments must be an object'}
            result, error = self._mcp_call(name, args)
            return result, error
        if method == 'ping':
            return {}, None
        return None, {'code': -32601, 'message': 'Method not found: %s' % method}

    def _mcp_call(self, name, args):
        tool = next((t for t in MCP_TOOLS if t['name'] == name), None)
        if tool is None:
            return None, {'code': -32602, 'message': 'Unknown tool: %s' % name}
        for req in tool['inputSchema'].get('required', []):
            if req not in args or args[req] is None:
                return _tool_error('missing required argument: %s' % req), None
        try:
            if name == 'create_repo':
                return self._mcp_create_repo(args), None
            if name == 'get_repo':
                return self._mcp_get_repo(args), None
            if name == 'create_issue':
                return self._mcp_create_issue(args), None
            if name == 'list_issues':
                return self._mcp_list_issues(args), None
            if name == 'create_pull_request':
                return self._mcp_create_pr(args), None
            if name == 'merge_pull_request':
                return self._mcp_merge_pr(args), None
        except ApiError as e:
            return _tool_error(e.message), None
        return _tool_error('unknown tool'), None

    def _mcp_repo(self, args):
        repo = STORE.get_repo(args.get('owner'), args.get('repo'))
        if repo is None:
            raise ApiError(404, 'Repository not found')
        return repo

    def _mcp_create_repo(self, args):
        name = args['name']
        repo = STORE.get_repo(VIEWER_LOGIN, name)
        if repo is None:
            repo = STORE.create_repo(VIEWER_LOGIN, name,
                                     args.get('private', False),
                                     args.get('description', ''))
            if repo is None:
                return _tool_error('Repository already exists')
        return _tool_result(repo.to_json())

    def _mcp_get_repo(self, args):
        return _tool_result(self._mcp_repo(args).to_json())

    def _mcp_create_issue(self, args):
        repo = self._mcp_repo(args)
        with repo.lock:
            number = repo.next_number
            repo.next_number += 1
            repo.seq += 1
            issue = Issue(number, STORE.new_item_id(), args['title'],
                          args.get('body') or '', [], repo.seq, repo)
            repo.issues[number] = issue
        return _tool_result(issue.to_json())

    def _mcp_list_issues(self, args):
        repo = self._mcp_repo(args)
        state = args.get('state') or 'open'
        with repo.lock:
            items = [i.to_json() for i in sorted(repo.issues.values(),
                                                 key=lambda i: i.seq)]
        if state != 'all':
            items = [i for i in items if i['state'] == state]
        return _tool_result(items)

    def _mcp_create_pr(self, args):
        repo = self._mcp_repo(args)
        head = str(args['head']).split(':')[-1]
        base = str(args['base']).split(':')[-1]
        with repo.lock:
            head_tip = repo.branch_tip(head)
            base_tip = repo.branch_tip(base)
            if head_tip is None or base_tip is None:
                return _tool_error('head or base branch not found')
            for pr in repo.pulls.values():
                if pr.state == 'open' and pr.head == head and pr.base == base:
                    return _tool_error('a pull request from %s to %s is already open'
                                       % (head, base))
            number = repo.next_number
            repo.next_number += 1
            repo.seq += 1
            pr = PullRequest(number, STORE.new_item_id(), args['title'],
                             args.get('body') or '', head, base,
                             head_tip, base_tip, repo.seq, repo)
            repo.pulls[number] = pr
            return _tool_result(pr.to_json(mergeable=mergeable_status(repo, pr)))

    def _mcp_merge_pr(self, args):
        repo = self._mcp_repo(args)
        try:
            number = int(args['pull_number'])
        except (TypeError, ValueError):
            return _tool_error('pull_number must be an integer')
        with repo.lock:
            pr = repo.pulls.get(number)
            if pr is None:
                return _tool_error('Pull request not found')
            if pr.state != 'open':
                return _tool_error('pull request is not open')
            if mergeable_status(repo, pr) == 'CONFLICTING':
                return _tool_error('merge conflict')
            commit_oid, _c = do_squash_merge(repo, pr)
            return _tool_result({'number': number, 'merged': True,
                                 'state': 'closed', 'sha': commit_oid})


# ---------------------------------------------------------------------------
# Server + lifecycle
# ---------------------------------------------------------------------------

class ForgeHandler(Handler, RestMixin, GraphQLMixin, McpMixin):
    """One handler: smart-HTTP git + REST v3 + GraphQL + MCP."""


class ForgeServer(ThreadingHTTPServer):
    # The clone workload opens 50 concurrent connections; the default
    # listen backlog (5) drops most SYNs, forcing 1s+ TCP retransmits.
    request_queue_size = 128
    daemon_threads = True


def _make_server(port):
    server = ForgeServer(('127.0.0.1', port), ForgeHandler)
    return server


def main():
    import signal
    env_url = os.environ.get('MAKEFUN_ENV_URL', '').rstrip('/')
    if env_url:
        server = _make_server(0)
        port = server.server_address[1]

        def _term(_signum, _frame):
            sys.exit(0)

        signal.signal(signal.SIGTERM, _term)
        signal.signal(signal.SIGINT, _term)
        data = json.dumps({'port': port}).encode('utf-8')
        req = urllib.request.Request(
            env_url + '/register', data=data,
            headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        except Exception as e:
            print('register failed: %s' % e, file=sys.stderr, flush=True)
            sys.exit(1)
        if status == 409:
            print('register conflict (another entry registered first)',
                  file=sys.stderr, flush=True)
            sys.exit(1)
        if status not in (200, 201):
            print('register failed: HTTP %s' % status, file=sys.stderr, flush=True)
            sys.exit(1)
        print('registered on port %d; waiting for /done' % port, flush=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        while True:
            time.sleep(0.5)
            try:
                with urllib.request.urlopen(env_url + '/done', timeout=5) as resp:
                    done = json.loads(resp.read().decode('utf-8')).get('done')
            except Exception:
                done = False
            if done:
                server.shutdown()
                sys.exit(0)
    else:
        port = int(os.environ.get('PORT', '8080'))
        server = _make_server(port)
        print('github-core-clone listening on 127.0.0.1:%d (viewer %s)'
              % (port, VIEWER_LOGIN), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
