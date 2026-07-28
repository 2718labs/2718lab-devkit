"""Deterministic local SQLite/WAL graph and verified blob store."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Iterable

from .canonical import canonical_hash, canonical_id, canonical_json
from .models import (
    AtlasEdge,
    AtlasNode,
    AtlasStatus,
    ConstraintSpec,
    DependencySpec,
    EdgeRelation,
    GraphQueryResult,
    ImplementationPacket,
    IngestionReceipt,
    NodeKind,
    RecipeManifest,
    SlotSpec,
    TemplateOperation,
    TestSpec,
)
from .security import (
    MAX_GRAPH_DEPTH,
    MAX_GRAPH_EDGES,
    MAX_GRAPH_NODES,
    MAX_PACKET_BYTES,
)


_HASH = re.compile(r"^sha256:([0-9a-f]{64})$")


class StoreConflictError(ValueError):
    """A safe, stable error raised when durable content conflicts."""

    code = "store_conflict"

    def __init__(self, message: str = "store conflict") -> None:
        super().__init__(message)


_SCHEMA = """
CREATE TABLE atlas_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE atlas_nodes (node_id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL, schema_version TEXT NOT NULL, extractor_id TEXT NOT NULL, extractor_version TEXT NOT NULL, provenance TEXT NOT NULL, source_hashes_json TEXT NOT NULL, created_at TEXT NOT NULL, superseded_at TEXT, quarantine_state TEXT NOT NULL);
CREATE TABLE atlas_edges (edge_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES atlas_nodes(node_id), target_id TEXT NOT NULL REFERENCES atlas_nodes(node_id), relation TEXT NOT NULL, payload_json TEXT NOT NULL, schema_version TEXT NOT NULL, provenance TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX atlas_edges_source ON atlas_edges(source_id, relation);
CREATE INDEX atlas_edges_target ON atlas_edges(target_id, relation);
CREATE TABLE atlas_recipes (recipe_id TEXT PRIMARY KEY REFERENCES atlas_nodes(node_id), intent_id TEXT NOT NULL, language TEXT NOT NULL, framework TEXT NOT NULL, layer TEXT NOT NULL CHECK (layer IN ('bundled', 'local')), version INTEGER NOT NULL, manifest_hash TEXT NOT NULL, repository_signature TEXT NOT NULL, state TEXT NOT NULL, supersedes_recipe_id TEXT);
CREATE INDEX atlas_recipe_match ON atlas_recipes(intent_id, language, framework, state);
CREATE TABLE atlas_recipe_nodes (recipe_id TEXT NOT NULL REFERENCES atlas_recipes(recipe_id), node_id TEXT NOT NULL REFERENCES atlas_nodes(node_id), PRIMARY KEY (recipe_id, node_id));
CREATE TABLE atlas_recipe_edges (recipe_id TEXT NOT NULL REFERENCES atlas_recipes(recipe_id), edge_id TEXT NOT NULL REFERENCES atlas_edges(edge_id), PRIMARY KEY (recipe_id, edge_id));
CREATE TABLE atlas_blobs (blob_hash TEXT PRIMARY KEY, size INTEGER NOT NULL, media_type TEXT NOT NULL);
CREATE TABLE atlas_packet_receipts (packet_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, packet_json TEXT NOT NULL, packet_hash TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE atlas_ingestion_receipts (ingestion_key TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, status TEXT NOT NULL, episode_id TEXT NOT NULL, recipe_id TEXT NOT NULL, reasons_json TEXT NOT NULL, created_at TEXT NOT NULL);
"""


class AtlasStore:
    def __init__(
        self, database_path: str | Path, cas_root: str | Path | None = None
    ) -> None:
        self._database_path = Path(database_path)
        self._cas_root = (
            Path(cas_root)
            if cas_root is not None
            else self._database_path.parent / "cas"
        )
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._cas_root.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._database_path, timeout=30.0)
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            exists = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='atlas_metadata'"
            ).fetchone()
            if exists is None:
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        self._conn.execute(statement)
            current = self._conn.execute(
                "SELECT value FROM atlas_metadata WHERE key='schema_version'"
            ).fetchone()
            if current is None:
                self._conn.execute(
                    "INSERT INTO atlas_metadata(key,value) VALUES ('schema_version','1')"
                )
            elif current[0] != "1":
                raise StoreConflictError("unsupported schema version")
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.close()

    def schema_version(self) -> int:
        return int(
            self._conn.execute(
                "SELECT value FROM atlas_metadata WHERE key='schema_version'"
            ).fetchone()[0]
        )

    def journal_mode(self) -> str:
        return str(self._conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    def foreign_keys_enabled(self) -> bool:
        return bool(self._conn.execute("PRAGMA foreign_keys").fetchone()[0])

    @staticmethod
    def _node_identity(node: AtlasNode) -> str:
        return canonical_id(
            node.kind.value,
            node.payload,
            schema_version=node.schema_version,
            extractor_id=node.extractor_id,
            extractor_version=node.extractor_version,
            provenance=node.provenance,
            source_hashes=node.source_hashes,
        )

    @staticmethod
    def _edge_identity(edge: AtlasEdge) -> str:
        return canonical_hash(
            {
                "relation": edge.relation.value,
                "source_id": edge.source_id,
                "target_id": edge.target_id,
                "schema_version": edge.schema_version,
                "provenance": edge.provenance,
                "payload": edge.payload,
            }
        )

    def put_nodes(self, nodes: Iterable[AtlasNode]) -> tuple[AtlasNode, ...]:
        items = tuple(nodes)
        for node in items:
            if self._node_identity(node) != node.node_id:
                raise StoreConflictError("node identity conflict")
        with self._conn:
            for node in items:
                immutable = (
                    node.kind.value,
                    canonical_json(node.payload),
                    node.schema_version,
                    node.extractor_id,
                    node.extractor_version,
                    node.provenance,
                    canonical_json(node.source_hashes),
                )
                row = self._conn.execute(
                    "SELECT kind,payload_json,schema_version,extractor_id,extractor_version,provenance,source_hashes_json FROM atlas_nodes WHERE node_id=?",
                    (node.node_id,),
                ).fetchone()
                if row is not None and tuple(row) != immutable:
                    raise StoreConflictError("node immutable payload conflict")
                if row is None:
                    self._conn.execute(
                        "INSERT INTO atlas_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            node.node_id,
                            *immutable,
                            node.created_at or "",
                            node.superseded_at,
                            node.quarantine_state or "",
                        ),
                    )
        return items

    def put_edges(self, edges: Iterable[AtlasEdge]) -> tuple[AtlasEdge, ...]:
        items = tuple(edges)
        for edge in items:
            if self._edge_identity(edge) != edge.edge_id:
                raise StoreConflictError("edge identity conflict")
            endpoints = self._conn.execute(
                "SELECT node_id,kind FROM atlas_nodes WHERE node_id IN (?,?)",
                (edge.source_id, edge.target_id),
            ).fetchall()
            if (
                len(endpoints) != 2
                or dict(endpoints).get(edge.source_id) != edge.source_kind.value
                or dict(endpoints).get(edge.target_id) != edge.target_kind.value
            ):
                raise StoreConflictError("edge endpoint conflict")
        with self._conn:
            for edge in items:
                immutable = (
                    edge.source_id,
                    edge.target_id,
                    edge.relation.value,
                    canonical_json(edge.payload),
                    edge.schema_version,
                    edge.provenance,
                )
                row = self._conn.execute(
                    "SELECT source_id,target_id,relation,payload_json,schema_version,provenance FROM atlas_edges WHERE edge_id=?",
                    (edge.edge_id,),
                ).fetchone()
                if row is not None and tuple(row) != immutable:
                    raise StoreConflictError("edge immutable payload conflict")
                if row is None:
                    self._conn.execute(
                        "INSERT INTO atlas_edges VALUES (?,?,?,?,?,?,?,?)",
                        (edge.edge_id, *immutable, edge.created_at or ""),
                    )
        return items

    def put_recipe(
        self,
        manifest: RecipeManifest,
        *,
        node_ids: Iterable[str] = (),
        edge_ids: Iterable[str] = (),
    ) -> str:
        linked_nodes = tuple(node_ids)
        linked_edges = tuple(edge_ids)
        immutable = (
            manifest.intent_id,
            manifest.language_name,
            manifest.framework_name or "",
            manifest.layer,
            manifest.version,
            manifest.manifest_hash,
            manifest.repository_signature,
            manifest.quarantine_state or "ready",
            manifest.superseded_ids[0] if manifest.superseded_ids else None,
        )
        with self._conn:
            recipe_row = self._conn.execute(
                "SELECT kind FROM atlas_nodes WHERE node_id=?", (manifest.recipe_id,)
            ).fetchone()
            if recipe_row is None or recipe_row[0] != NodeKind.RECIPE.value:
                raise StoreConflictError("recipe node conflict")
            row = self._conn.execute(
                "SELECT intent_id,language,framework,layer,version,manifest_hash,repository_signature,state,supersedes_recipe_id FROM atlas_recipes WHERE recipe_id=?",
                (manifest.recipe_id,),
            ).fetchone()
            if row is not None and tuple(row) != immutable:
                raise StoreConflictError("recipe conflict")
            if row is None:
                self._conn.execute(
                    "INSERT INTO atlas_recipes VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (manifest.recipe_id, *immutable),
                )
            for node_id in linked_nodes:
                if (
                    self._conn.execute(
                        "SELECT 1 FROM atlas_nodes WHERE node_id=?", (node_id,)
                    ).fetchone()
                    is None
                ):
                    raise StoreConflictError("recipe node link conflict")
                self._conn.execute(
                    "INSERT OR IGNORE INTO atlas_recipe_nodes VALUES (?,?)",
                    (manifest.recipe_id, node_id),
                )
            for edge_id in linked_edges:
                row_edge = self._conn.execute(
                    "SELECT source_id,target_id FROM atlas_edges WHERE edge_id=?",
                    (edge_id,),
                ).fetchone()
                if (
                    row_edge is None
                    or row_edge[0] not in linked_nodes
                    or row_edge[1] not in linked_nodes
                ):
                    raise StoreConflictError("recipe edge link conflict")
                self._conn.execute(
                    "INSERT OR IGNORE INTO atlas_recipe_edges VALUES (?,?)",
                    (manifest.recipe_id, edge_id),
                )
        return manifest.recipe_id

    def _blob_path(self, blob_hash: str) -> Path:
        match = _HASH.fullmatch(blob_hash)
        if not match:
            raise StoreConflictError("blob hash conflict")
        digest = match.group(1)
        return self._cas_root / "sha256" / digest[:2] / digest[2:]

    @staticmethod
    def _read_blob_file(path: Path) -> bytes:
        try:
            if not path.is_file():
                raise StoreConflictError("blob path conflict")
            return path.read_bytes()
        except OSError as exc:
            raise StoreConflictError("blob path conflict") from exc

    def put_blob(
        self,
        blob_hash: str,
        content: bytes,
        media_type: str = "application/octet-stream",
    ) -> str:
        if not isinstance(content, bytes):
            raise StoreConflictError("blob hash conflict")
        actual = "sha256:" + hashlib.sha256(content).hexdigest()
        if blob_hash != actual:
            raise StoreConflictError("blob hash conflict")
        path = self._blob_path(blob_hash)
        created_path = False
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            path.parent.mkdir(parents=True, exist_ok=True)
            path_exists = path.exists()
            path_is_file = path.is_file() if path_exists else False
            row = self._conn.execute(
                "SELECT size,media_type FROM atlas_blobs WHERE blob_hash=?",
                (blob_hash,),
            ).fetchone()
            if (row is None) == path_exists:
                raise StoreConflictError("blob consistency conflict")
            if path_exists and not path_is_file:
                raise StoreConflictError("blob path conflict")
            values = (len(content), media_type)
            if row is not None and tuple(row) != values:
                raise StoreConflictError("blob metadata conflict")
            if path_exists:
                if self._read_blob_file(path) != content:
                    raise StoreConflictError("blob filesystem conflict")
            else:
                temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
                try:
                    with open(temp, "xb") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temp, path)
                    created_path = True
                except OSError as exc:
                    raise StoreConflictError("blob write conflict") from exc
                finally:
                    try:
                        if temp.exists():
                            temp.unlink()
                    except OSError as exc:
                        raise StoreConflictError("blob write conflict") from exc
                if self._read_blob_file(path) != content:
                    raise StoreConflictError("blob verification conflict")
            if row is None:
                self._conn.execute(
                    "INSERT INTO atlas_blobs VALUES (?,?,?)", (blob_hash, *values)
                )
            self._conn.commit()
        except OSError as exc:
            self._conn.rollback()
            if created_path:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise StoreConflictError("blob path conflict") from exc
        except Exception:
            self._conn.rollback()
            if created_path:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise
        return blob_hash

    def read_blob(self, blob_hash: str) -> bytes:
        path = self._blob_path(blob_hash)
        row = self._conn.execute(
            "SELECT size,media_type FROM atlas_blobs WHERE blob_hash=?", (blob_hash,)
        ).fetchone()
        try:
            path_is_file = path.is_file()
        except OSError as exc:
            raise StoreConflictError("blob path conflict") from exc
        if row is None or not path_is_file:
            raise StoreConflictError("blob consistency conflict")
        content = self._read_blob_file(path)
        if row[0] != len(content):
            raise StoreConflictError("blob size conflict")
        if "sha256:" + hashlib.sha256(content).hexdigest() != blob_hash:
            raise StoreConflictError("blob hash conflict")
        return content

    def put_packet(self, packet: ImplementationPacket, *, created_at: str = "") -> str:
        payload = canonical_json(packet.to_dict())
        digest = canonical_hash(packet.to_dict())
        with self._conn:
            row = self._conn.execute(
                "SELECT snapshot_id,packet_json,packet_hash FROM atlas_packet_receipts WHERE packet_id=?",
                (packet.packet_id,),
            ).fetchone()
            values = (packet.snapshot_id, payload, digest)
            if row is not None and tuple(row) != values:
                raise StoreConflictError("packet receipt conflict")
            if row is None:
                self._conn.execute(
                    "INSERT INTO atlas_packet_receipts VALUES (?,?,?,?,?)",
                    (packet.packet_id, *values, created_at),
                )
        return packet.packet_id

    def get_packet(self, packet_id: str) -> ImplementationPacket | None:
        row = self._conn.execute(
            "SELECT packet_json FROM atlas_packet_receipts WHERE packet_id=?",
            (packet_id,),
        ).fetchone()
        if row is None:
            return None
        value = json.loads(row[0])
        value["evidence_windows"] = tuple(value["evidence_windows"])
        value["operations"] = tuple(
            TemplateOperation(**item) for item in value["operations"]
        )
        value["slots"] = tuple(SlotSpec(**item) for item in value["slots"])
        value["constraints"] = tuple(
            ConstraintSpec(**item) for item in value["constraints"]
        )
        value["dependencies"] = tuple(
            DependencySpec(**item) for item in value["dependencies"]
        )
        value["tests"] = tuple(
            TestSpec(tuple(item["argv"]), item["expected_exit_code"])
            for item in value["tests"]
        )
        return ImplementationPacket(**value)

    def put_ingestion_receipt(self, receipt: IngestionReceipt) -> str:
        values = (
            receipt.payload_hash,
            receipt.status.value,
            receipt.episode_id,
            receipt.recipe_id or "",
            canonical_json(receipt.reasons),
            receipt.created_at,
        )
        with self._conn:
            row = self._conn.execute(
                "SELECT payload_hash,status,episode_id,recipe_id,reasons_json,created_at FROM atlas_ingestion_receipts WHERE ingestion_key=?",
                (receipt.ingestion_key,),
            ).fetchone()
            if row is not None and tuple(row) != values:
                raise StoreConflictError("ingestion receipt conflict")
            if row is None:
                self._conn.execute(
                    "INSERT INTO atlas_ingestion_receipts VALUES (?,?,?,?,?,?,?)",
                    (receipt.ingestion_key, *values),
                )
        return receipt.ingestion_key

    def get_ingestion_receipt(self, ingestion_key: str) -> IngestionReceipt | None:
        row = self._conn.execute(
            "SELECT payload_hash,status,episode_id,recipe_id,reasons_json,created_at FROM atlas_ingestion_receipts WHERE ingestion_key=?",
            (ingestion_key,),
        ).fetchone()
        return (
            None
            if row is None
            else IngestionReceipt(
                ingestion_key,
                row[0],
                AtlasStatus(row[1]),
                row[2],
                row[3] or None,
                tuple(json.loads(row[4])),
                row[5],
            )
        )

    def recipes_for_intent(
        self,
        intent_id: str,
        *,
        language: str | None = None,
        framework: str | None = None,
        state: str | None = None,
        limit: int = MAX_GRAPH_NODES + 1,
    ) -> tuple[str, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 0 < limit <= MAX_GRAPH_NODES + 1
        ):
            raise ValueError("invalid recipe discovery limit")
        clauses: list[str] = ["intent_id=?"]
        args: list[object] = [intent_id]
        for column, value in (
            ("language", language),
            ("framework", framework),
            ("state", state),
        ):
            if value is not None:
                clauses.append(column + "=?")
                args.append(value)
        args.append(limit)
        return tuple(
            row[0]
            for row in self._conn.execute(
                "SELECT recipe_id FROM atlas_recipes WHERE "
                + " AND ".join(clauses)
                + " ORDER BY recipe_id LIMIT ?",
                args,
            )
        )

    def _node(self, node_id: str) -> AtlasNode:
        row = self._conn.execute(
            "SELECT node_id,kind,payload_json,schema_version,extractor_id,extractor_version,provenance,source_hashes_json,created_at,superseded_at,quarantine_state FROM atlas_nodes WHERE node_id=?",
            (node_id,),
        ).fetchone()
        return AtlasNode(
            row[0],
            NodeKind(row[1]),
            json.loads(row[2]),
            row[3],
            row[4],
            row[5],
            row[6],
            tuple(json.loads(row[7])),
            row[8] or None,
            row[9],
            row[10] or None,
        )

    def _edge(self, edge_id: str) -> AtlasEdge:
        row = self._conn.execute(
            "SELECT e.edge_id,e.relation,e.source_id,e.target_id,s.kind,t.kind,e.payload_json,e.schema_version,e.provenance,e.created_at FROM atlas_edges e JOIN atlas_nodes s ON s.node_id=e.source_id JOIN atlas_nodes t ON t.node_id=e.target_id WHERE e.edge_id=?",
            (edge_id,),
        ).fetchone()
        return AtlasEdge(
            row[0],
            EdgeRelation(row[1]),
            row[2],
            row[3],
            NodeKind(row[4]),
            NodeKind(row[5]),
            json.loads(row[6]),
            row[7],
            row[8],
            row[9] or None,
        )

    def graph_query(
        self,
        root_node_ids: Iterable[str],
        *,
        max_nodes: int = MAX_GRAPH_NODES,
        max_edges: int = MAX_GRAPH_EDGES,
        max_depth: int = MAX_GRAPH_DEPTH,
        byte_budget: int = MAX_PACKET_BYTES,
        node_kinds: Iterable[NodeKind] | None = None,
        relations: Iterable[EdgeRelation] | None = None,
    ) -> GraphQueryResult:
        if not (
            0 < max_nodes <= MAX_GRAPH_NODES
            and 0 < max_edges <= MAX_GRAPH_EDGES
            and 0 < max_depth <= MAX_GRAPH_DEPTH
            and 0 < byte_budget <= MAX_PACKET_BYTES
        ):
            raise ValueError("invalid graph budget")
        allowed_kinds = (
            None
            if node_kinds is None
            else {
                item.value if isinstance(item, NodeKind) else str(item)
                for item in node_kinds
            }
        )
        allowed_relations = (
            None
            if relations is None
            else {
                item.value if isinstance(item, EdgeRelation) else str(item)
                for item in relations
            }
        )
        roots = tuple(root_node_ids)
        chosen = set()
        frontier = sorted(set(roots))
        truncated = False
        for root in frontier:
            if (
                self._conn.execute(
                    "SELECT 1 FROM atlas_nodes WHERE node_id=?", (root,)
                ).fetchone()
                is not None
            ):
                if len(chosen) >= max_nodes:
                    truncated = True
                    break
                chosen.add(root)
        selected_edges: set[str] = set()
        for _depth in range(max_depth):
            next_frontier: set[str] = set()
            for node_id in sorted(frontier):
                rows = self._conn.execute(
                    "SELECT edge_id,source_id,target_id,relation FROM atlas_edges WHERE source_id=? OR target_id=? ORDER BY edge_id",
                    (node_id, node_id),
                ).fetchall()
                for edge_id, source, target, relation in rows:
                    other = target if source == node_id else source
                    if (
                        allowed_relations is not None
                        and relation not in allowed_relations
                    ):
                        continue
                    if allowed_kinds is not None:
                        kind = self._conn.execute(
                            "SELECT kind FROM atlas_nodes WHERE node_id=?", (other,)
                        ).fetchone()[0]
                        if kind not in allowed_kinds:
                            continue
                    if other not in chosen and len(chosen) >= max_nodes:
                        truncated = True
                        continue
                    if (
                        edge_id not in selected_edges
                        and len(selected_edges) >= max_edges
                    ):
                        truncated = True
                        continue
                    chosen.add(other)
                    selected_edges.add(edge_id)
                    next_frontier.add(other)
            frontier = sorted(next_frontier)
        # The final frontier is present at the permitted depth; report omitted
        # descendants instead of making a depth-limited result look complete.
        for node_id in frontier:
            for _edge_id, source, target, relation in self._conn.execute(
                "SELECT edge_id,source_id,target_id,relation FROM atlas_edges WHERE source_id=? OR target_id=? ORDER BY edge_id",
                (node_id, node_id),
            ):
                other = target if source == node_id else source
                if allowed_relations is not None and relation not in allowed_relations:
                    continue
                if allowed_kinds is not None:
                    kind = self._conn.execute(
                        "SELECT kind FROM atlas_nodes WHERE node_id=?", (other,)
                    ).fetchone()[0]
                    if kind not in allowed_kinds:
                        continue
                if _edge_id not in selected_edges or other not in chosen:
                    truncated = True
        nodes = tuple(self._node(item) for item in sorted(chosen))
        edges = tuple(
            edge
            for edge in (self._edge(item) for item in sorted(selected_edges))
            if edge.source_id in chosen and edge.target_id in chosen
        )
        while nodes or edges:
            result = GraphQueryResult(nodes, edges, truncated)
            if len(canonical_json(result.to_dict()).encode("utf-8")) <= byte_budget:
                return result
            truncated = True
            if edges:
                edges = edges[:-1]
            else:
                root_ids = set(roots)
                removable = [node for node in nodes if node.node_id not in root_ids]
                if removable:
                    remove_id = removable[-1].node_id
                    nodes = tuple(node for node in nodes if node.node_id != remove_id)
                    present = {node.node_id for node in nodes}
                    edges = tuple(
                        edge
                        for edge in edges
                        if edge.source_id in present and edge.target_id in present
                    )
                else:
                    return GraphQueryResult((), (), True)
        return GraphQueryResult((), (), truncated)
