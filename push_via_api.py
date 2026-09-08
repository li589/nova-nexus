# -*- coding: utf-8 -*-
"""通过 gh CLI（GitHub API）把本地仓库完整同步到远端 main 分支。
用于沙箱屏蔽 github.com:443（git push 不可用）时的替代推送方案。"""
import base64
import json
import os
import subprocess
import sys

REPO = "li589/nova-nexus"
BRANCH = "main"
ROOT = os.path.dirname(os.path.abspath(__file__))


def gh_api(path, method="GET", payload=None, jq=None):
    cmd = ["gh", "api", path, "--method", method]
    if jq:
        cmd += ["--jq", jq]
    stdin_data = None
    if payload is not None:
        cmd += ["--input", "-"]
        stdin_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    p = subprocess.run(cmd, input=stdin_data, capture_output=True, cwd=ROOT)
    if p.returncode != 0:
        sys.stderr.write(p.stderr.decode("utf-8", "replace") + "\n")
        raise SystemExit(f"gh api {path} failed")
    out = p.stdout.decode("utf-8", "replace").strip()
    if jq or method == "GET":
        return out
    try:
        return json.loads(out)
    except Exception:
        return out


def main():
    msg = sys.argv[1] if len(sys.argv) > 1 else "chore: sync from local"

    # 1) 远端分支 HEAD
    remote_sha = gh_api(f"repos/{REPO}/git/ref/heads/{BRANCH}", jq=".object.sha")
    print("remote HEAD:", remote_sha)

    # 2) 本地已跟踪文件清单
    files = subprocess.run(
        ["git", "ls-files", "-z"], capture_output=True, cwd=ROOT
    ).stdout.decode("utf-8").split("\0")
    files = [f for f in files if f]
    print("files:", len(files))

    # 3) 逐个创建 blob
    tree = []
    for rel in files:
        with open(os.path.join(ROOT, rel), "rb") as f:
            data = f.read()
        b64 = base64.b64encode(data).decode("ascii")
        res = gh_api(
            f"repos/{REPO}/git/blobs",
            method="POST",
            payload={"content": b64, "encoding": "base64"},
        )
        tree.append({"path": rel, "mode": "100644", "type": "blob", "sha": res["sha"]})
        print(f"  blob {rel} ({len(data)}B) -> {res['sha'][:8]}")

    # 4) 建树（base_tree 保留远端已有但未在本仓库跟踪的文件）
    tree_res = gh_api(
        f"repos/{REPO}/git/trees",
        method="POST",
        payload={"base_tree": remote_sha, "tree": tree},
    )
    tree_sha = tree_res["sha"]
    print("tree:", tree_sha)

    # 5) 提交
    commit_res = gh_api(
        f"repos/{REPO}/git/commits",
        method="POST",
        payload={"message": msg, "tree": tree_sha, "parents": [remote_sha]},
    )
    commit_sha = commit_res["sha"]
    print("commit:", commit_sha)

    # 6) 更新引用
    gh_api(
        f"repos/{REPO}/git/refs/heads/{BRANCH}",
        method="PATCH",
        payload={"sha": commit_sha},
    )
    print("pushed ->", commit_sha)


if __name__ == "__main__":
    main()
