#!/usr/bin/env python3
"""
A hash of the code a build runs, for a cache key: the given files, every
pipeline module they import, directly or not, and every pipeline file those
modules name in a string, such as train_protection.json. A new import then
rebuilds the cached output without a list to keep up to date.

Usage: cache_key.py FILE... [--list]
"""
import ast
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def inputs(paths):
    """The files paths depend on, sorted."""
    seen, todo = set(), [os.path.abspath(p) for p in paths]
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        if not path.endswith(".py"):
            continue
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "/" not in node.value:
                data = os.path.join(HERE, node.value)
                if node.value.endswith(".json") and os.path.isfile(data):
                    todo.append(data)
                continue
            else:
                continue
            for name in names:
                module = os.path.join(HERE, name.split(".")[0] + ".py")
                if os.path.isfile(module):
                    todo.append(module)
    return sorted(seen)


def main():
    args = [a for a in sys.argv[1:] if a != "--list"]
    if not args:
        sys.exit(__doc__)
    files = inputs(args)
    if "--list" in sys.argv:
        print("\n".join(os.path.relpath(p) for p in files))
        return
    h = hashlib.md5()
    for path in files:
        h.update(os.path.relpath(path, os.path.dirname(HERE)).encode() + b"\0")
        with open(path, "rb") as f:
            h.update(hashlib.md5(f.read()).digest())
    print(h.hexdigest())


if __name__ == "__main__":
    main()
