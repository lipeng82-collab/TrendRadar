#!/usr/bin/env python3
"""Read-only adapter: TrendRadar daily SQLite -> human-review topic cards.

Only first-pass relevance is scored; no article verification is claimed.
No networking, AI requests, secret access, sending, or publishing.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
from ipaddress import ip_address
import json
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

BEIJING = timezone(timedelta(hours=8))


PROFILE_RULES = {
    "personal": {
        "看懂商业": ("商业模式", "现金流", "财报", "利润", "毛利", "定价", "订单", "客户", "算力",
                 "词元", "token", "智能体", "op c", "opc", "创业", "营收", "采购", "ai", "消费", "供应链"),
        "看懂国企": ("国企", "央企", "国资", "国有资本", "兴湘", "债券", "资产盘活", "平台转型",
                 "工业软件", "制造业", "产业链", "投资", "融资", "成本"),
        "第二课堂": ("教育", "学校", "教师", "大学", "高中", "高考", "就业", "人才", "职业",
                 "学习", "历史", "规则", "规划", "城市", "医保", "养老"),
    },
    "institution": {
        "湖南国资国企": ("湖南", "长沙", "国企", "国资", "国有资本", "央企", "兴湘", "产业政策", "资产盘活"),
        "产业与经营": ("产业链", "智能制造", "工程机械", "轨道交通", "科技金融", "词元",
                 "op c", "opc", "算力", "智能体", "订单", "采购", "项目", "技改", "应用场景"),
        "人才与公共服务": ("人才", "就业", "教育", "医疗", "交通", "公共服务", "职业教育", "人才集团"),
    },
}
DETAIL_WORDS = ("增长", "下降", "亏损", "首单", "首次", "计划", "发布", "获批", "招标",
                "公告", "数据", "调查", "价格", "利润", "成本", "收入", "改革", "监管", "试点",
                "落地", "合同", "争议", "延期", "退出", "回款", "财政")
TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
            "fbclid", "gclid", "spm"}
ACCESS_PARAMS = {"token", "access_token", "api_key", "apikey", "key", "signature",
                 "sig", "auth", "authorization", "password", "secret"}
PROFILE_LIMITS = {"personal": 3, "institution": 2}


def canonical_url(url: str) -> str:
    try:
        s = urlsplit(url.strip())
        if s.scheme.lower() not in {"http", "https"} or not s.netloc or s.username or s.password:
            return ""
        host = (s.hostname or "").lower()
        if "." not in host or host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal", ".localhost")):
            return ""
        try:
            if not ip_address(host).is_global:
                return ""
        except ValueError:
            pass  # Public domain names are allowed without DNS/network lookup.
        pairs = parse_qsl(s.query, keep_blank_values=True)
        if any(k.lower() in ACCESS_PARAMS or k.lower().startswith(("x-amz-", "x-goog-"))
               for k, _ in pairs):
            return ""  # Access-bearing links must never appear in exported cards.
        query = urlencode([(k, v) for k, v in pairs
                           if k.lower() not in TRACKING], doseq=True)
        return urlunsplit((s.scheme.lower(), s.netloc.lower(), s.path.rstrip("/") or "/",
                           query, ""))
    except ValueError:
        return ""


def normalize_title(title: str) -> str:
    return re.sub(r"[\W_]+", "", title, flags=re.UNICODE).casefold()


def read_db_rows(db: Path, kind: str, day: str) -> list[dict]:
    if not db.is_file():
        return []
    conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    try:
        if kind == "news":
            rows = conn.execute(
                """SELECT n.title, n.url, n.first_crawl_time, n.rank, p.name
                     FROM news_items n
                     LEFT JOIN platforms p ON p.id = n.platform_id
                     ORDER BY n.rank ASC, n.first_crawl_time DESC LIMIT 2500"""
            )
        else:
            rows = conn.execute(
                """SELECT r.title, r.url, COALESCE(r.published_at,r.first_crawl_time),
                          NULL, f.name
                     FROM rss_items r
                     LEFT JOIN rss_feeds f ON f.id = r.feed_id
                     ORDER BY r.first_crawl_time DESC LIMIT 2500"""
            )
        return [
            {"title": row[0], "url": row[1] or "", "observed_at": row[2] or day,
             "rank": row[3], "source": row[4] or kind, "kind": kind, "day": day}
            for row in rows
        ]
    finally:
        conn.close()


def fetch_rows(repo: Path, end: date, days: int) -> list[dict]:
    rows = []
    readable_databases = 0
    for offset in range(days):
        d = (end - timedelta(days=offset)).isoformat()
        for kind in ("news", "rss"):
            db = repo / "output" / kind / f"{d}.db"
            if not db.is_file():
                continue
            try:
                rows.extend(read_db_rows(db, kind, d))
                readable_databases += 1
            except sqlite3.DatabaseError as exc:
                print(f"警告：跳过损坏或不兼容的库 {db.name}（{kind}）：{exc}", file=sys.stderr)
    if not readable_databases:
        raise ValueError("所选日期没有可读取的本地数据库；尚未完成采集，不能判为零条合格选题")
    return rows


def contains_term(text: str, term: str) -> bool:
    if term.isascii():
        return bool(re.search(r"(?<![a-z0-9])" + re.escape(term.casefold()) + r"(?![a-z0-9])", text))
    return term in text


def rank_relevance(item: dict, profile: str) -> tuple[int, str, list[str]]:
    title = (item.get("title") or "").strip()
    lower = title.casefold()
    matches = []
    for label, words in PROFILE_RULES[profile].items():
        tokens = [word for word in words if contains_term(lower, word)]
        if tokens:
            matches.append((label, tokens))
    if not matches:
        return 0, "", []
    matches.sort(key=lambda p: len(p[1]), reverse=True)
    category = matches[0][0]
    term_count = sum(min(3, len(tokens)) for _, tokens in matches)
    topic_points = min(65, 20 + term_count * 11)
    detail_points = min(20, sum(5 for x in DETAIL_WORDS if x in title))
    source_points = 10 if canonical_url(item.get("url") or "") else 0
    # Popularity is a clue, not evidence quality or factual confidence.
    rank = item.get("rank")
    visibility_points = 5 if isinstance(rank, int) and rank <= 10 else 0
    return min(100, topic_points + detail_points + source_points + visibility_points), category, [
        word for _, words in matches for word in words
    ][:8]


def build_cards(rows: list[dict], profile: str, max_cards: int) -> list[dict]:
    if profile not in PROFILE_RULES:
        raise ValueError("未知内容流")
    if not 1 <= max_cards <= PROFILE_LIMITS[profile]:
        raise ValueError(f"{profile} 每次最多 {PROFILE_LIMITS[profile]} 条")
    pool = []
    for row in rows:
        title = (row.get("title") or "").strip()
        if not title or not canonical_url(row.get("url") or ""):
            continue  # Link required: a title without a verifiable entry point is not a candidate.
        score, column, terms = rank_relevance(row, profile)
        if score < 35:
            continue
        row_copy = dict(row)
        row_copy.update({"score": score, "column": column, "matched_terms": terms})
        pool.append(row_copy)
    pool.sort(key=lambda r: (r["score"], r.get("day", ""), str(r.get("observed_at", ""))),
              reverse=True)
    seen_urls = set()
    seen_titles = set()
    cards = []
    for row in pool:
        url = canonical_url(row["url"])
        title_key = normalize_title(row["title"])
        if url in seen_urls or title_key in seen_titles:
            continue
        seen_urls.add(url)
        seen_titles.add(title_key)
        card_id = hashlib.sha256((profile + "|" + url).encode("utf-8")).hexdigest()[:12]
        cards.append({
            "id": card_id,
            "profile": profile,
            "title": row["title"],
            "source": row["source"],
            "source_type": "RSS" if row["kind"] == "rss" else "热榜",
            "url": url,
            "seen_date": row["day"],
            "source_time_raw": row["observed_at"],
            "primary_source_verified": False,
            "fact_verified": False,
            "relevance_score_only": row["score"],
            "matched_terms": row["matched_terms"],
            "proposed_column": row["column"] if profile == "personal" else "智库观点候选",
            "decision_linkage": "B（待验证）" if profile == "institution" else "不适用",
            "why_research": f"标题涉及：{'、'.join(row['matched_terms'][:4])}。是否有值得解释的反差、成本或选择权，需要看原文。",
            "questions": [
                "发布者、原始出处、时间、效力或财务口径是什么？",
                "谁实际支付、承担风险或拥有调整权？有无反例？",
                ("与湖南的具体传导机制、服务对象和决策事项是否成立？"
                 if profile == "institution" else
                 "读者日常会遇到什么问题，适合哪一栏？"),
            ],
            "next_action": "用 Codex 核对第一手来源与反证；作者决定是否立项",
            "approval": "未审核，不得发布",
        })
        if len(cards) == max_cards:
            break
    return cards


def markdown(cards: list[dict], profile: str, end: date) -> str:
    heading = "thinking tank L 叔｜个人 IP" if profile == "personal" else "湘江研究院｜智库观点"
    lines = [f"# {heading} 候选选题卡（{end.isoformat()}）", "",
             "> 自动筛出的仅是公开新闻线索；相关性分数不是证据强度、商业机会或写作许可。",
             "> 请按随包 CODEX_TASK.md 核验原始出处、反证和个人/机构边界。无合格选题可以留空。", ""]
    if not cards:
        return "\n".join(lines + ["本次没有符合初筛条件的候选；不要为凑数量造题。", ""])
    for idx, card in enumerate(cards, 1):
        lines.extend([
            f"## {idx}. {card['title']}", "",
            f"- 原线索：[{card['source']}]({card['url']})（{card['source_type']}，抓取日 {card['seen_date']}）",
            f"- 归属：{card['proposed_column']}；初筛相关性 {card['relevance_score_only']}/100",
            f"- 关注缘由：{card['why_research']}",
            f"- 核验状态：原始出处未核实；事实未核实；{card['approval']}",
        ])
        if profile == "institution":
            lines.append("- 决策关联：B（待验证）；不能据此推断已与某服务对象相关")
        lines.append("- 待查：")
        lines.extend(f"  - {q}" for q in card["questions"])
        lines.append("")
    return "\n".join(lines)


def export_cards(repo: Path, out: Path, profile: str, end: date, days: int, max_cards: int) -> tuple[Path, Path, int]:
    repo, out = repo.resolve(), out.resolve()
    if out == repo or repo in out.parents:
        raise ValueError("候选卡输出目录须在仓库外，避免误提交原始新闻线索")
    if not 1 <= days <= 14:
        raise ValueError("--days 须在 1~14")
    marker = repo / "config" / "custom" / "tt_l_profile.json"
    if not marker.is_file():
        raise ValueError("TrendRadar 尚无内容流标记；先按预演/部署流程绑定此目录")
    assigned = json.loads(marker.read_text(encoding="utf-8"))["profile"]
    if assigned != profile:
        raise ValueError(f"目录已绑定 {assigned} 流，拒绝跨流读取")
    rows = fetch_rows(repo, end, days)
    cards = build_cards(rows, profile, max_cards)
    destination = out / profile
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(BEIJING).strftime("%Y%m%d_%H%M%S_%f")
    json_file = destination / f"{end.isoformat()}_{stamp}.json"
    md_file = destination / f"{end.isoformat()}_{stamp}.md"
    # New filenames each run: never overwrite a human-edited review card.
    json_file.write_text(json.dumps(cards, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_file.write_text(markdown(cards, profile, end), encoding="utf-8")
    return json_file, md_file, len(cards)


def main() -> int:
    parser = argparse.ArgumentParser(description="从 TrendRadar 本地 SQLite 生成研究候选卡（无发布）")
    parser.add_argument("--repo", required=True, type=Path, help="TrendRadar 根目录")
    parser.add_argument("--profile", choices=PROFILE_RULES, default="personal")
    parser.add_argument("--out", required=True, type=Path, help="输出根目录（自动分 personal/institution）")
    parser.add_argument("--date", type=date.fromisoformat,
                        default=datetime.now(BEIJING).date())
    parser.add_argument("--days", type=int, default=2)
    parser.add_argument("--max", type=int, dest="max_cards")
    args = parser.parse_args()
    count = args.max_cards if args.max_cards is not None else (3 if args.profile == "personal" else 2)
    if count < 1 or count > PROFILE_LIMITS[args.profile] or args.days < 1 or args.days > 14:
        parser.error(f"--max 须在 1~{PROFILE_LIMITS[args.profile]}，--days 须在 1~14")
    try:
        j, m, n = export_cards(args.repo.expanduser().resolve(), args.out.expanduser().resolve(),
                               args.profile, args.date, args.days, count)
    except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 2
    print(f"已生成 {n} 条待研究线索（不足不补）。")
    print(f"JSON: {j}\nMarkdown: {m}")
    print("未读取或传输内部资料；未自动成文或发布。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
