# -*- coding: utf-8 -*-
"""把本地仓库完整同步到远端分支（git push 被封锁时的兜底）。

用法：
    python push_via_api.py "commit message"            # 推当前分支
    python push_via_api.py "commit message" main       # 显式指定分支
    REPO=owner/name python push_via_api.py "msg"       # 目录里没有 remote 时显式指定

流程：blob → tree(base_tree=远端HEAD) → commit → patch ref

要点：
- REPO 自动从 `origin` remote 推断
- token 取自 `gh auth token`，HTTP 用 urllib 直连 api.github.com
- **GitHub API 会间歇性返回 500 / 连接重置**（实测 curl 连试 3 次第 3 次才 201），
  因此内置指数退避重试：只对 5xx / 429 / 网络异常重试，4xx 直接失败
- 不要用 `gh api --input -` 传大 JSON，会报 "unexpected end of JSON input"
"""
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
RETRY = 6
_TOKEN = None


def git(*args, binary=False):
    p = subprocess.run(["git"] + list(args), capture_output=True, cwd=ROOT)
    if p.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {p.stderr.decode('utf-8', 'replace')[:200]}")
    return p.stdout if binary else p.stdout.decode("utf-8", "replace")


def infer_repo():
    env = os.environ.get("REPO")
    if env:
        return env
    try:
        url = git("remote", "get-url", "origin").strip()
    except SystemExit:
        raise SystemExit("无法读取 origin remote，请用 REPO=owner/name 显式指定")
    m = re.search(r"github\.com[:/]+([^/]+)/([^/\s]+?)(?:\.git)?$", url)
    if not m:
        raise SystemExit(f"无法从 remote 解析 owner/repo: {url}")
    return f"{m.group(1)}/{m.group(2)}"


def get_token():
    p = subprocess.run(["gh", "auth", "token"], capture_output=True, cwd=ROOT)
    if p.returncode != 0:
        raise SystemExit("取不到 gh token，请先 gh auth login")
    return p.stdout.decode().strip()


def api(base, path, method="GET", payload=None, retry=RETRY):
    global _TOKEN
    if _TOKEN is None:
        _TOKEN = get_token()
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    last = ""
    for i in range(retry):
        req = urllib.request.Request(
            f"https://api.github.com/repos/{base}{path}",
            data=data, method=method,
            headers={"Authorization": "Bearer " + _TOKEN,
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json",
                     "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                body = r.read().decode("utf-8", "replace")
            return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
            if e.code < 500 and e.code != 429:
                break                      # 4xx 是请求本身的问题，重试无意义
        except Exception as e:             # 网络抖动 / 连接重置
            last = f"{type(e).__name__}: {e}"
        wait = min(2 ** i, 20)
        print(f"    retry {i+1}/{retry} after {wait}s ({last})", flush=True)
        time.sleep(wait)
    sys.stderr.write(last + "\n")
    raise SystemExit(f"API failed: {method} {path}")


def main():
    if len(sys.argv) < 2:
        raise SystemExit('用法: python push_via_api.py "commit message" [branch]')
    msg = sys.argv[1]
    branch = sys.argv[2] if len(sys.argv) > 2 else git("rev-parse", "--abbrev-ref", "HEAD").strip()
    repo = infer_repo()
    print(f"repo: {repo}  branch: {branch}")

    remote_sha = api(repo, f"/git/ref/heads/{branch}")["object"]["sha"]
    print("remote HEAD:", remote_sha)

    files = [f for f in git("ls-files", "-z").split("\0") if f]
    print("files:", len(files))

    tree = []
    for rel in files:
        with open(os.path.join(ROOT, rel), "rb") as f:
            data = f.read()
        res = api(repo, "/git/blobs", "POST",
                  {"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"})
        tree.append({"path": rel, "mode": "100644", "type": "blob", "sha": res["sha"]})
        print(f"  blob {rel} ({len(data)}B) -> {res['sha'][:8]}")

    tree_sha = api(repo, "/git/trees", "POST",
                   {"base_tree": remote_sha, "tree": tree})["sha"]
    commit_sha = api(repo, "/git/commits", "POST",
                     {"message": msg, "tree": tree_sha, "parents": [remote_sha]})["sha"]
    api(repo, f"/git/refs/heads/{branch}", "PATCH", {"sha": commit_sha, "force": False})
    print("pushed ->", commit_sha)


if __name__ == "__main__":
    main()