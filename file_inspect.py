"""File inspection for inter-subtask communication.

After a subtask completes, inspect its output files to extract type,
size, and schema information. This is passed to downstream subtasks
so they know exactly what data is available without exploring.

Zero LLM calls — just filesystem reads.
"""
import csv
import json
import os


def sniff_file(path: str) -> dict:
    """Inspect a file and return metadata: type, size, preview, schema.

    Returns a dict with:
        path: filename
        type: json|csv|text
        size: bytes
        lines: line count
        preview: first few meaningful bytes
        schema: keys/columns if structured
    """
    info = {
        "path": os.path.basename(path),
        "type": "text",
        "size": 0,
        "lines": 0,
    }

    try:
        info["size"] = os.path.getsize(path)
    except OSError:
        return info

    if info["size"] == 0:
        info["type"] = "empty"
        return info

    try:
        with open(path, "r", errors="replace") as f:
            content = f.read(4096)  # read first 4KB only
    except OSError:
        return info

    lines = content.splitlines()
    info["lines"] = len(lines)

    # Try JSON
    try:
        if info["size"] > 4096:
            with open(path, "r", errors="replace") as full_f:
                content = full_f.read()
        data = json.loads(content)
        info["type"] = "json"
        if isinstance(data, list):
            info["type"] = "json_array"
            info["items"] = len(data)
            if data and isinstance(data[0], dict):
                info["schema"] = list(data[0].keys())
                info["preview"] = f"[{len(data)} objects, keys: {', '.join(info['schema'][:8])}]"
            else:
                info["preview"] = f"[{len(data)} items]"
        elif isinstance(data, dict):
            info["type"] = "json_object"
            info["schema"] = list(data.keys())
            info["preview"] = f"{{keys: {', '.join(info['schema'][:8])}}}"
        return info
    except (json.JSONDecodeError, OSError):
        pass

    # Try CSV
    if lines and "," in lines[0]:
        try:
            dialect = csv.Sniffer().sniff(lines[0])
            reader = csv.reader(lines[:2], dialect)
            header = next(reader, None)
            if header and len(header) >= 2:
                info["type"] = "csv"
                info["schema"] = header
                info["preview"] = f"CSV, {info['lines']} rows, columns: {', '.join(header[:8])}"
                return info
        except csv.Error:
            pass

    # Plain text
    info["type"] = "text"
    preview_lines = lines[:3]
    info["preview"] = "\n".join(preview_lines)[:200]
    return info


def sniff_session_files(session_dir: str, subtask_id: str) -> list[dict]:
    """Inspect all output files from a subtask in the session directory.

    Looks for files that:
    - Were written by this subtask (contain subtask_id in name)
    - Are user-created data files (not starting with _)
    """
    results = []
    if not os.path.isdir(session_dir):
        return results

    for fname in sorted(os.listdir(session_dir)):
        path = os.path.join(session_dir, fname)
        if not os.path.isfile(path):
            continue
        # Include subtask-specific files and any user-created files
        # Skip internal files unless they belong to this subtask
        if fname.startswith("_") and subtask_id not in fname:
            continue
        info = sniff_file(path)
        if info.get("size", 0) > 0:
            results.append(info)

    return results


def format_file_context(files: list[dict]) -> str:
    """Format file info into a compact context string for the LLM."""
    if not files:
        return ""

    parts = []
    for f in files:
        preview = f.get("preview", "")
        schema = f.get("schema")
        if schema:
            parts.append(f"  {f['path']} — {f['type']}, {f.get('items', f.get('lines', '?'))} items, "
                        f"keys: {', '.join(schema[:6])}")
        elif preview:
            parts.append(f"  {f['path']} — {f['type']}, {f['lines']} lines: {preview[:80]}")
        else:
            parts.append(f"  {f['path']} — {f['type']}, {f['size']} bytes")

    return "Available files:\n" + "\n".join(parts)
