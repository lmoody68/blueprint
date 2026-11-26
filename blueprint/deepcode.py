"""BLUEPRINT — Deep Code analysis (the Craft Anything "inspect its code and files" dimension).

BLUEPRINT's default teardown reasons from PUBLIC signals (HTTP/HTML/fingerprints). This module goes deeper,
like CA: point it at the ACTUAL code — a local folder, OR a git repo it shallow-clones — read the real source
tree (skipping deps/binaries), and extract grounded evidence from the files themselves: languages, frameworks
(from real manifests), structure, entry points, and key file contents. The teardown is then grounded in real
code, not inferred from the outside.

Legitimate use only — analyze code you have the right to (your own, a friend's with permission, an open-source
repo). Matches CA's own posture: it reads files, it does NOT crack DRM, logins, or anti-cheat.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

# Directories and files that are dependencies, build output, binaries or VCS — never the product's own source.
SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "env", "__pycache__", "dist", "build", ".next",
             "target", "vendor", ".idea", ".vscode", "coverage", ".pytest_cache", "out", ".turbo",
             ".cache", "bin", "obj", ".gradle", "Pods", ".dart_tool"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".bmp", ".woff", ".woff2", ".ttf",
            ".eot", ".otf", ".mp4", ".webm", ".mp3", ".wav", ".zip", ".gz", ".tar", ".rar", ".7z", ".pdf",
            ".lock", ".map", ".wasm", ".so", ".dll", ".dylib", ".exe", ".bin", ".class", ".o", ".a",
            ".pyc", ".jar", ".war", ".db", ".sqlite", ".ds_store"}
MANIFESTS = {"package.json", "requirements.txt", "pyproject.toml", "setup.py", "go.mod", "cargo.toml",
             "pom.xml", "build.gradle", "composer.json", "gemfile", "pubspec.yaml", "dockerfile",
             "docker-compose.yml", ".env.example", "next.config.js", "vite.config.js", "tsconfig.json",
             "svelte.config.js", "nuxt.config.ts", "manage.py", "makefile", "procfile"}
SOURCE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".rb", ".php", ".vue",
              ".svelte", ".html", ".css", ".scss", ".json", ".yaml", ".yml", ".toml", ".md", ".sql",
              ".sh", ".cs", ".swift", ".dart", ".c", ".cpp", ".h", ".ex", ".exs", ".clj", ".graphql"}

MAX_TREE = 400          # paths listed in the tree
MAX_FILE_BYTES = 60000  # per-file content cap
MAX_FILES_CONTENT = 60  # how many files we keep full-ish content for
MAX_TOTAL_BYTES = 1_200_000
CLONE_TIMEOUT = int(os.getenv("BLUEPRINT_CLONE_TIMEOUT", "120"))

_GIT_RE = re.compile(r"^(https?://|git@).*(github\.com|gitlab\.com|bitbucket\.org|codeberg\.org)", re.I)


def is_git_url(s: str) -> bool:
    s = (s or "").strip()
    return bool(_GIT_RE.match(s)) or s.endswith(".git")


def ingest(source: str) -> dict:
    """Ingest a real codebase from a local folder path OR a git URL (shallow clone). Returns grounded evidence
    {origin, file_count, tree, languages, manifests, files} or {error}. Clones to a temp dir, always cleaned up."""
    source = (source or "").strip()
    if not source:
        return {"error": "Provide a local folder path or a git repo URL."}
    tmp = None
    try:
        if is_git_url(source):
            tmp = tempfile.mkdtemp(prefix="bp_clone_")
            r = subprocess.run(["git", "clone", "--depth", "1", "--single-branch", source, tmp],
                               capture_output=True, text=True, timeout=CLONE_TIMEOUT)
            if r.returncode != 0:
                return {"error": f"git clone failed: {(r.stderr or r.stdout or 'unknown error')[:220]}"}
            return _scan(tmp, source)
        root = os.path.abspath(os.path.expanduser(source))
        if not os.path.isdir(root):
            return {"error": f"Not a folder (and not a git URL): {source[:120]}"}
        return _scan(root, root)
    except subprocess.TimeoutExpired:
        return {"error": f"git clone timed out after {CLONE_TIMEOUT}s (repo too large or slow)."}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {str(e)[:160]}"}
    finally:
        if tmp and os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)


def _scan(root: str, origin: str) -> dict:
    tree: list[str] = []
    files: list[dict] = []
    langs: dict[str, int] = {}
    manifests: dict[str, str] = {}
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # prune skip-dirs and hidden dirs in place so os.walk never descends into them
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS and not d.startswith(".")]
        rel = os.path.relpath(dirpath, root)
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if ext in SKIP_EXT or fn.lower() in {".ds_store"}:
                continue
            rp = os.path.normpath(os.path.join("" if rel == "." else rel, fn)).replace("\\", "/")
            if len(tree) < MAX_TREE:
                tree.append(rp)
            full = os.path.join(dirpath, fn)
            try:
                sz = os.path.getsize(full)
            except OSError:
                continue
            if ext:
                langs[ext] = langs.get(ext, 0) + 1
            if sz > MAX_FILE_BYTES or total >= MAX_TOTAL_BYTES or len(files) >= MAX_FILES_CONTENT * 3:
                continue
            is_manifest = fn.lower() in MANIFESTS
            if not (is_manifest or ext in SOURCE_EXT):
                continue
            try:
                with open(full, encoding="utf-8", errors="ignore") as fh:
                    content = fh.read(MAX_FILE_BYTES)
            except Exception:
                continue
            total += len(content)
            if is_manifest:
                manifests[rp] = content[:8000]
            else:
                files.append({"path": rp, "content": content})

    # rank source files: entry/route/model-like and shallow files first (most informative for a teardown)
    def _rank(f: dict) -> tuple:
        p = f["path"].lower()
        key = 0
        if re.search(r"(^|/)(main|index|app|server|__init__|routes?|router|api|models?|schema|urls?)\.", p):
            key -= 5
        return (key, p.count("/"), len(f["content"]) * -1)

    files.sort(key=_rank)
    return {
        "origin": origin,
        "file_count": len(tree),
        "truncated": len(tree) >= MAX_TREE,
        "tree": tree[:300],
        "languages": dict(sorted(langs.items(), key=lambda x: -x[1])[:12]),
        "manifests": manifests,
        "files": files[:MAX_FILES_CONTENT],
    }
