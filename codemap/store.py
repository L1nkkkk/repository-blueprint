"""Single-project transactional storage for the v0.1 reference protocol.

Stores one graph document per database; large-graph sharding is not implemented.
"""

from contextlib import contextmanager
import json
import sqlite3

from .core import apply_batch, canonical, claim_task, recover_expired, renew_task, retry_task, require, validate_graph


class GraphStore:
    def __init__(self, path):
        self.path = str(path)
        with self._connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS graph (id INTEGER PRIMARY KEY CHECK(id=1), document TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS views (id TEXT PRIMARY KEY, document TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS snapshot_history (revision INTEGER PRIMARY KEY, snapshot_id TEXT NOT NULL, document TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS source_texts (sha256 TEXT PRIMARY KEY, content TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS structure_cache (id TEXT PRIMARY KEY, document TEXT NOT NULL)")

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self, graph):
        validate_graph(graph)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            require(db.execute("SELECT 1 FROM graph").fetchone() is None, "store already initialized")
            db.execute("INSERT INTO graph VALUES (1, ?)", (canonical(graph),))

    def read(self):
        with self._connection() as db:
            row = db.execute("SELECT document FROM graph WHERE id=1").fetchone()
            require(row is not None, "store is not initialized")
            return json.loads(row[0])

    def _change(self, transform):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT document FROM graph WHERE id=1").fetchone()
            require(row is not None, "store is not initialized")
            updated = transform(json.loads(row[0]))
            db.execute("UPDATE graph SET document=? WHERE id=1", (canonical(updated),))
            return updated

    def claim(self, task_id, worker, *, expected_revision, now, lease_seconds=300):
        return self._change(lambda g: claim_task(g, task_id, worker, expected_revision=expected_revision, now=now, lease_seconds=lease_seconds))

    def commit(self, batch, *, now):
        return self._change(lambda g: apply_batch(g, batch, now=now))

    def update_snapshot(self, fresh, *, expected_revision, plan_id, old_texts=None, new_texts=None, renames=()):
        from .updates import migrate_snapshot
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT document FROM graph WHERE id=1').fetchone()
            require(row is not None, 'store is not initialized')
            graph = json.loads(row[0])
            if graph.get('last_update', {}).get('plan_id') == plan_id:
                return graph
            updated = migrate_snapshot(graph, fresh, expected_revision=expected_revision, plan_id=plan_id,
                                       old_texts=old_texts, new_texts=new_texts, renames=renames)
            if updated != graph:
                db.execute('INSERT INTO snapshot_history VALUES (?, ?, ?)',
                           (graph['project']['revision'], graph['project']['snapshot_id'], row[0]))
                db.execute('UPDATE graph SET document=? WHERE id=1', (canonical(updated),))
                db.executemany('INSERT OR IGNORE INTO source_texts VALUES (?, ?)', (new_texts or {}).items())
            return updated

    def save_source_texts(self, texts):
        with self._connection() as db:
            db.executemany('INSERT OR IGNORE INTO source_texts VALUES (?, ?)', texts.items())

    def cached_structure(self, keys):
        with self._connection() as db:
            return {key: json.loads(row[0]) for key in keys
                    if (row := db.execute('SELECT document FROM structure_cache WHERE id=?', (key,)).fetchone())}

    def save_structure(self, key, document):
        # A derived cache never owns task leases, evidence or graph revisions.
        with self._connection() as db:
            db.execute('INSERT OR REPLACE INTO structure_cache VALUES (?, ?)', (key, canonical(document)))

    def source_texts(self, hashes):
        with self._connection() as db:
            return {hash: row[0] for hash in set(hashes)
                    if (row := db.execute('SELECT content FROM source_texts WHERE sha256=?', (hash,)).fetchone())}

    def save_prepared(self, batch):
        """Immutable draft storage; never changes graph progress or its revision."""
        from .core import digest
        id = 'prepared:' + digest(batch)
        with self._connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS prepared_batches (id TEXT PRIMARY KEY, document TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO prepared_batches VALUES (?, ?)', (id, canonical(batch)))
        return id

    def read_prepared(self, id):
        with self._connection() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='prepared_batches'").fetchone()
            row = db.execute('SELECT document FROM prepared_batches WHERE id=?', (id,)).fetchone() if exists else None
            require(row is not None, 'prepared batch not found in this project')
            return json.loads(row[0])

    def snapshot_history(self):
        with self._connection() as db:
            return [{'revision': revision, 'snapshot_id': snapshot} for revision, snapshot in
                    db.execute('SELECT revision, snapshot_id FROM snapshot_history ORDER BY revision DESC')]

    def read_snapshot(self, revision):
        with self._connection() as db:
            row = db.execute('SELECT document FROM snapshot_history WHERE revision=?', (revision,)).fetchone()
            require(row is not None, 'archived snapshot not found')
            return json.loads(row[0])

    def recover(self, *, expected_revision, now):
        return self._change(lambda g: recover_expired(g, expected_revision=expected_revision, now=now))

    def renew(self, task_id, lease_id, *, expected_revision, now, lease_seconds=300):
        return self._change(lambda g: renew_task(g, task_id, lease_id, expected_revision=expected_revision, now=now, lease_seconds=lease_seconds))

    def retry(self, task_id, *, expected_revision):
        return self._change(lambda g: retry_task(g, task_id, expected_revision=expected_revision))

    def set_paused(self, paused, *, expected_revision):
        require(type(paused) is bool, "paused must be boolean")

        def change(graph):
            require(type(expected_revision) is int and graph["project"]["revision"] == expected_revision, "stale project revision")
            graph["project"]["execution"] = "paused" if paused else "active"
            graph["project"]["revision"] += 1
            return validate_graph(graph)

        return self._change(change)

    def save_view(self, view_id, document):
        require(isinstance(view_id, str) and view_id.strip(), "view id required")
        require(isinstance(document, dict), "view must be an object")
        with self._connection() as db:
            db.execute("INSERT INTO views VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET document=excluded.document", (view_id, canonical(document)))

    def read_view(self, view_id):
        with self._connection() as db:
            row = db.execute("SELECT document FROM views WHERE id=?", (view_id,)).fetchone()
            return json.loads(row[0]) if row else None
