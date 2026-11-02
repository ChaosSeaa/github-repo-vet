#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub 仓库体检：判定真实项目 / 空壳占位 / 蹭名仓库。

用法:
    python repo_vet.py OWNER/REPO
    python repo_vet.py https://github.com/OWNER/REPO
    python repo_vet.py OWNER/REPO --json

未认证的 GitHub API 限速为每小时 60 次，单次运行约消耗 6-8 次。
"""

import json
import re
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
UA = "repo-vet/1.0"
TIMEOUT = 20


def get(path):
    """请求 GitHub API，返回 (data, headers, status)。"""
    req = urllib.request.Request(
        API + path,
        headers={
            "User-Agent": UA,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw), dict(resp.headers), resp.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            data = json.loads(body)
        except Exception:
            data = {"message": body[:200]}
        return data, dict(e.headers or {}), e.code
    except Exception as e:  # noqa: BLE001
        return {"message": "网络错误: %s" % e}, {}, 0


def parse_target(arg):
    """从 URL 或 owner/repo 解析出 (owner, repo)。"""
    arg = arg.strip().rstrip("/")
    m = re.search(r"github\.com[/:]([^/]+)/([^/#?]+)", arg)
    if m:
        owner, repo = m.group(1), m.group(2)
    elif "/" in arg:
        owner, repo = arg.split("/", 1)
        repo = repo.split("?")[0].split("#")[0]
    else:
        return None, None
    if repo.endswith(".git"):
        repo = repo[:-4]
    return owner, repo


def count_commits(owner, repo):
    """用 Link header 推断提交总数；失败返回 None。"""
    _, headers, status = get("/repos/%s/%s/commits?per_page=1" % (owner, repo))
    if status != 200:
        return None
    link = headers.get("Link") or headers.get("link") or ""
    m = re.search(r'[?&]page=(\d+)>; rel="last"', link)
    if m:
        return int(m.group(1))
    return 1


def list_root(owner, repo):
    data, _, status = get("/repos/%s/%s/contents/" % (owner, repo))
    if status != 200 or not isinstance(data, list):
        return None
    return [{"name": it.get("name", ""), "type": it.get("type", ""), "size": it.get("size", 0)} for it in data]


def find_official(owner, repo):
    """找同名仓库里 star 最高的那个，用于识别蹭名。"""
    data, _, status = get(
        "/search/repositories?q=%s+in:name&sort=stars&order=desc&per_page=1" % repo
    )
    if status != 200 or not isinstance(data, dict):
        return None
    items = data.get("items") or []
    if not items:
        return None
    top = items[0]
    return {
        "full_name": top.get("full_name"),
        "stars": top.get("stargazers_count", 0),
        "size": top.get("size", 0),
        "language": top.get("language"),
    }


def fmt_size(kb):
    if kb is None:
        return "?"
    if kb >= 1024:
        return "%.1f MB" % (kb / 1024.0)
    return "%d KB" % kb


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    as_json = "--json" in sys.argv
    no_search = "--no-search" in sys.argv

    if not args:
        print(__doc__)
        return 2

    owner, repo = parse_target(args[0])
    if not owner:
        print("无法解析参数，请使用 OWNER/REPO 或 https://github.com/OWNER/REPO")
        return 2

    info, _, status = get("/repos/%s/%s" % (owner, repo))
    if status == 404:
        print("仓库不存在：%s/%s（HTTP 404）" % (owner, repo))
        print("请核对 owner 拼写，或确认它是否为私有仓库。")
        return 1
    if status == 403:
        print("被 GitHub 限速或拒绝访问：%s" % info.get("message", ""))
        print("未认证请求限速为每小时 60 次，稍后重试。")
        return 1
    if status != 200:
        print("请求失败（HTTP %s）：%s" % (status, info.get("message", "")))
        return 1

    size = info.get("size", 0)
    stars = info.get("stargazers_count", 0)
    forks = info.get("forks_count", 0)
    is_fork = bool(info.get("fork"))
    created = (info.get("created_at") or "")[:10]
    desc = (info.get("description") or "").strip()
    full = info.get("full_name", "%s/%s" % (owner, repo))
    official_owner = owner.lower() == repo.lower()

    commits = count_commits(owner, repo)
    root = list_root(owner, repo)
    root_names = [f["name"] for f in root] if root else []
    has_readme = any(n.lower().startswith("readme") for n in root_names)

    user, _, us = get("/users/%s" % owner)
    account_age_days = None
    repo_count = None
    empty_ratio = None
    if us == 200:
        from datetime import datetime, timezone

        ca = user.get("created_at") or ""
        if ca:
            try:
                dt = datetime.strptime(ca[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                account_age_days = (datetime.now(timezone.utc) - dt).days
            except ValueError:
                pass
        repo_count = user.get("public_repos")

    if repo_count is not None and repo_count > 0:
        repos, _, rs = get("/users/%s/repos?per_page=100" % owner)
        if rs == 200 and isinstance(repos, list) and repos:
            empties = sum(1 for r in repos if (r.get("size") or 0) <= 1)
            empty_ratio = empties / float(len(repos))

    official = None if (no_search or official_owner) else find_official(owner, repo)

    # ---- 打分 ----
    score = 0
    flags = []

    def hit(points, text):
        nonlocal score
        score += points
        flags.append(text)

    if size <= 1:
        hit(3, "体积 %s，无实质内容" % fmt_size(size))
    if commits is not None and commits <= 1:
        hit(2, "仅 %d 次提交，占位式建仓" % commits)
    if root is not None and not has_readme:
        hit(2, "根目录没有 README")
    placeholder = (
        root is not None
        and len(root_names) == 1
        and root_names[0].lower().replace(".md", "") in (repo.lower(), "readme")
    )
    if placeholder:
        hit(3, "根目录仅一个占位文件：%s" % root_names[0])
    if account_age_days is not None and account_age_days < 90:
        hit(1, "账号注册仅 %d 天" % account_age_days)
    if repo_count and repo_count >= 10 and empty_ratio is not None and empty_ratio > 0.7:
        hit(2, "账号 %d 个仓库中 %.0f%% 为空" % (repo_count, empty_ratio * 100))
    if official and official.get("full_name", "").lower() != full.lower():
        o_stars = official.get("stars", 0)
        if o_stars > 1000 and (stars * 50 < o_stars):
            hit(3, "同名官方仓库 %s 有 %s star，本仓库仅 %d" % (official["full_name"], o_stars, stars))

    if score >= 7:
        verdict = "空壳或占位"
        level = "high"
    elif score >= 4:
        verdict = "可疑"
        level = "mid"
    elif score >= 1:
        verdict = "基本正常"
        level = "low"
    else:
        verdict = "真实项目"
        level = "ok"

    result = {
        "repo": full,
        "verdict": verdict,
        "level": level,
        "score": score,
        "metrics": {
            "size_kb": size,
            "commits": commits,
            "stars": stars,
            "forks": forks,
            "is_fork": is_fork,
            "created_at": created,
            "description": desc,
            "has_readme": has_readme,
            "root_files": root_names[:12],
            "root_file_count": len(root_names),
            "account_age_days": account_age_days,
            "account_repos": repo_count,
            "account_empty_ratio": None if empty_ratio is None else round(empty_ratio, 3),
            "official_candidate": official,
        },
        "flags": flags,
    }

    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    # ---- 文本报告 ----
    line = "─" * 52
    print("仓库：%s" % full)
    print(line)
    print("描述        %s" % (desc[:60] if desc else "（无）"))
    print("体积        %-14s %s" % (fmt_size(size), "⚠" if size <= 1 else "✓"))
    print("提交数      %-14s %s" % (commits if commits is not None else "?",
                                    "⚠" if (commits is not None and commits <= 1) else ""))
    print("根目录      %-14s %s" % ("%d 项" % len(root_names) if root else "?",
                                    ",".join(root_names[:4]) if root_names else ""))
    print("README      %-14s %s" % ("有" if has_readme else "无", "" if has_readme else "⚠"))
    print("Star/Fork   %d / %d" % (stars, forks))
    print("是否 fork   %s" % ("是" if is_fork else "否"))
    if account_age_days is not None:
        print("账号年龄    %d 天" % account_age_days)
    if repo_count is not None:
        extra = ""
        if empty_ratio is not None:
            extra = "（%.0f%% 为空）" % (empty_ratio * 100)
        print("账号仓库    %d 个%s" % (repo_count, extra))
    if official:
        print("同名最高    %s  %s star" % (official["full_name"], official["stars"]))
    print(line)

    if flags:
        print("可疑信号：")
        for f in flags:
            print("  ⚠ %s" % f)
        print(line)

    print("结论：%s（评分 %d）" % (verdict, score))

    if level in ("high", "mid"):
        cand = official["full_name"] if official else None
        print()
        print("建议：先向用户说明该仓库的实际状态，再决定是否改用官方源。")
        if cand and cand.lower() != full.lower():
            print("      同名官方候选：https://github.com/%s" % cand)
        print("      不要用模型对同名项目的记忆去填充内容。")

    return 0 if level in ("ok", "low") else 3


if __name__ == "__main__":
    sys.exit(main())
