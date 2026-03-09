"""Async wrapper around the qmd CLI for markdown search and embedding."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from loguru import logger


class QMDClient:
    """Thin async client for the qmd on-device markdown search engine."""

    def __init__(
        self,
        collection_name: str,
        collection_path: Path,
        mask: str = "**/*.md",
    ):
        self.collection_name = collection_name
        self.collection_path = collection_path.resolve()
        self.mask = mask
        self._qmd_bin = shutil.which("qmd")

    @property
    def available(self) -> bool:
        return self._qmd_bin is not None

    async def _run(self, *args: str, timeout: float = 30) -> str:
        if not self._qmd_bin:
            return ""
        try:
            proc = await asyncio.create_subprocess_exec(
                self._qmd_bin,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            if proc.returncode != 0:
                err = stderr.decode(errors="replace").strip()
                logger.warning("qmd {} exited {}: {}", args[0] if args else "?", proc.returncode, err[:200])
                return ""
            return stdout.decode(errors="replace")
        except asyncio.TimeoutError:
            logger.warning("qmd {} timed out after {}s", args[0] if args else "?", timeout)
            return ""
        except Exception as e:
            logger.error("qmd {} failed: {}", args[0] if args else "?", e)
            return ""

    async def ensure_collection(self) -> None:
        """Create the qmd collection if it doesn't already exist."""
        if not self.available:
            return
        output = await self._run("collection", "list")
        if self.collection_name in output:
            return
        await self._run(
            "collection",
            "add",
            str(self.collection_path),
            "--name",
            self.collection_name,
            "--mask",
            self.mask,
        )
        logger.info("Created qmd collection '{}' at {}", self.collection_name, self.collection_path)

    async def update(self) -> None:
        """Re-index all collections."""
        await self._run("update", timeout=120)

    async def embed(self, force: bool = False) -> None:
        """Create vector embeddings for indexed documents."""
        args = ["embed"]
        if force:
            args.append("-f")
        await self._run(*args, timeout=120)

    async def reindex(self) -> None:
        """Convenience: update then embed."""
        await self.update()
        await self.embed()

    async def query(
        self,
        query: str,
        n: int = 5,
        collection: str | None = None,
    ) -> list[dict[str, Any]]:
        """Semantic search with query expansion + reranking. Returns result dicts."""
        args = ["query", query, "--json", "-n", str(n)]
        col = collection or self.collection_name
        if col:
            args.extend(["-c", col])
        output = await self._run(*args, timeout=30)
        return self._parse_json_results(output)

    async def vsearch(
        self,
        query: str,
        n: int = 5,
        collection: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fast vector similarity search (no reranking). Returns result dicts."""
        args = ["vsearch", query, "--json", "-n", str(n)]
        col = collection or self.collection_name
        if col:
            args.extend(["-c", col])
        output = await self._run(*args, timeout=15)
        return self._parse_json_results(output)

    async def search(self, query: str, n: int = 5) -> list[dict[str, Any]]:
        """BM25 keyword search (no LLM). Returns result dicts."""
        args = ["search", query, "--json", "-n", str(n), "-c", self.collection_name]
        output = await self._run(*args, timeout=15)
        return self._parse_json_results(output)

    @staticmethod
    def _parse_json_results(output: str) -> list[dict[str, Any]]:
        """Extract the JSON array from qmd output (may have non-JSON lines before it)."""
        if not output:
            return []
        # Find the JSON array in the output — qmd prints status lines before it
        start = output.find("[")
        if start == -1:
            return []
        try:
            return json.loads(output[start:])
        except json.JSONDecodeError:
            return []
