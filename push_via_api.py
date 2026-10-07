# -*- coding: utf-8 -*-
"""通过 GitHub API 把本地仓库完整同步到远端分支（git push 被网络封锁时的兜底）。

用法：python push_via_api.py "commit message"

流程：blob → tree(base_tree=远端HEAD) → commit → patch ref
- token 取自 `gh auth token`
- GitHub API 偶发 5xx / 连接重置，内置指数退避重试
- 二进制文件走 base64
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = "li589/nova-nexus"
BRANCH = "main"
ROOT = os.path.dirname(os.path.abspath(__file__))
API = f"https://api.github.com/repos/{REPO}"
RETRY = 6

_TOKEN = None


def get_token():
    p = subprocess.run(["gh", "auth", "token"], capture_output=True, cwd=ROOT)
    if p.returncode != 0:
        raise SystemExit("取不到 gh token，请先 gh auth login")
    return p.stdout.decode().strip()


def api(path, method="GET", payload=None, retry=RETRY):
    global _TOKEN
    if _TOKEN is None:
        _TOKEN = get_token()
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    last = ""
    for i in range(retry):
        req = urllib.request.Request(
            API + path, data=data, method=method,
            headers={"Authorization": "Bearer " + _TOKEN,
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json",
                     "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read().decode("utf-8", "replace")
            return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
            if e.code < 500 and e.code != 429:
                break                      # 4xx 是请求本身的问题，重试无意义
        except Exception as e:              # 网络抖动 / 连接重置
            last = f"{type(e).__name__}: {e}"
        wait = min(2 ** i, 20)
        print(f"    retry {i+1}/{retry} after {wait}s ({last})", flush=True)
        time.sleep(wait)
    sys.stderr.write(last + "\n")
    raise SystemExit(f"API failed: {method} {path}")


def main():
    msg = sys.argv[1] if len(sys.argv) > 1 else "chore: sync from local"

    remote_sha = api(f"/git/ref/heads/{BRANCH}")["object"]["sha"]
    print("remote HEAD:", remote_sha)

    files = [f for f in subprocess.run(["git", "ls-files", "-z"], capture_output=True,
                                       cwd=ROOT).stdout.decode("utf-8").split("\0") if f]
    print("files:", len(files))

    tree = []
    for rel in files:
        with open(os.path.join(ROOT, rel), "rb") as f:
            data = f.read()
        res = api("/git/blobs", "POST",
                  {"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"})
        tree.append({"path": rel, "mode": "100644", "type": "blob", "sha": res["sha"]})
        print(f"  blob {rel} ({len(data)}B) -> {res['sha'][:8]}")

    tree_sha = api("/git/trees", "POST",
                   {"base_tree": remote_sha, "tree": tree})["sha"]
    commit_sha = api("/git/commits", "POST",
                     {"message": msg, "tree": tree_sha, "parents": [remote_sha]})["sha"]
    api(f"/git/refs/heads/{BRANCH}", "PATCH", {"sha": commit_sha, "force": False})
    print("pushed ->", commit_sha)


if __name__ == "__main__":
    main()