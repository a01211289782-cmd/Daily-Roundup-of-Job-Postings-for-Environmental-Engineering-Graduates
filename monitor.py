#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
高校教师招聘 + 企业环境工程/EHS招聘 追踪脚本

数据源：
  1. 高校人才网每日汇总（gaoxiaojob.com/daily） —— 服务端渲染的静态页面，覆盖全国高校/
     科研院所/部分企业招聘公告，按学科关键词筛选高校侧结果
  2. 百度/必应网页搜索 —— 用于补充企业环境工程/EHS岗位招聘信息（best-effort，
     搜索引擎可能返回验证码或空结果，属正常现象，不代表脚本出错）

设计说明：
  - 高校人才网的主搜索页是 Vue.js 前端动态渲染，无法直接抓取；但其"每日汇总"
    栏目（/daily/detail/xxxx.html）是纯静态 HTML，可稳定抓取。
  - 猎聘（liepin.com）robots.txt 明确禁止自动化访问，故未采用；
    智联招聘/前程无忧等大平台反爬机制极强，同样未纳入。
  - 关键词匹配基于标题文本，可能漏掉标题未明确写出学科名称的广义人才引进公告
    （如"XX大学2026年诚聘海内外高层次人才"），这是标题级匹配的固有局限。
"""

import requests
import json
import os
import re
import hashlib
import time
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.header import Header
from bs4 import BeautifulSoup
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==================== 配置：高校/科研岗位学科关键词 ====================

DISCIPLINE_KEYWORDS = [
    "环境科学与工程", "环境工程", "环境科学",
    "自然地理学", "自然地理", "区域环境学", "区域环境",
    "生态环境", "环境健康", "大气环境", "水环境", "水污染",
    "土壤修复", "环境监测", "环境影响评价", "环境规划", "环境管理",
    "地理科学", "人文地理", "地理信息", "流域生态", "生态修复",
    "环境地学", "环境生态", "污染控制",
]

# ==================== 配置：企业侧 环境工程/EHS 关键词（用于日报企业招聘分类 + 搜索引擎） ====================

EHS_KEYWORDS = [
    "环境工程师", "环保工程师", "EHS", "安全环保", "环境健康安全",
    "环保技术", "环境合规", "环评工程师", "污染治理", "环保设备工程师",
    "环境管理体系", "HSE",
]

# 用于百度/必应搜索的关键词组合
SEARCH_KEYWORDS = [
    "环境工程师 招聘",
    "EHS工程师 招聘",
    "环保工程师 招聘 环境工程",
]

# ==================== 配置：抓取范围 ====================

# 每次检查最近 N 期"每日汇总"，避免遗漏偶发的发布延迟/网络问题
RECENT_DIGEST_COUNT = 7

# ==================== 邮件配置 ====================

EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "true").lower() not in ("false", "0", "no")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.qq.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SENDER = os.getenv("SENDER_EMAIL", "")
PASSWORD = os.getenv("EMAIL_PASSWORD", "")
RECEIVER = os.getenv("RECEIVER_EMAIL", "")

HISTORY_FILE = "job_history.json"
HISTORY_MAX = 2000

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
})


# ==================== URL标准化 ====================

def normalize_gaoxiaojob_url(href):
    """标准化高校人才网链接，兼容相对路径、协议相对路径和完整URL"""
    href = (href or "").strip()

    if not href:
        return ""

    if href.startswith("http://") or href.startswith("https://"):
        return href

    if href.startswith("//"):
        return "https:" + href

    if href.startswith("/"):
        return "https://www.gaoxiaojob.com" + href

    return "https://www.gaoxiaojob.com/" + href


# ==================== 历史记录（去重） ====================

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
            history.setdefault("seen_keys", [])
            return history
        except (json.JSONDecodeError, IOError):
            pass
    return {"seen_keys": [], "last_check": None}


def save_history(history):
    history.setdefault("seen_keys", [])
    history["seen_keys"] = history["seen_keys"][-HISTORY_MAX:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def make_dedup_key(source, identifier):
    return hashlib.md5(f"{source}|{identifier}".encode("utf-8")).hexdigest()


# ==================== 关键词匹配 ====================

def match_keywords(title, keywords):
    return [kw for kw in keywords if kw in title]


# ==================== 数据源：高校人才网每日汇总 ====================

def fetch_recent_digest_urls():
    """从 daily.html 提取最近 N 期日报详情页链接"""
    urls = []

    try:
        resp = session.get(
            "https://www.gaoxiaojob.com/daily.html",
            timeout=20,
            verify=False
        )

        if resp.status_code != 200:
            print(f"  [高校人才网-日报列表] HTTP {resp.status_code}")
            return urls

        resp.encoding = resp.apparent_encoding or "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")
        seen = set()

        for a in soup.find_all("a", href=re.compile(r"/daily/detail/\d+\.html")):
            href = a["href"]
            full_url = normalize_gaoxiaojob_url(href)

            if full_url and full_url not in seen:
                seen.add(full_url)
                urls.append(full_url)

            if len(urls) >= RECENT_DIGEST_COUNT:
                break

        print(f"  [高校人才网-日报列表] 找到 {len(urls)} 期最新日报")

    except Exception as e:
        print(f"  [高校人才网-日报列表] 失败: {str(e)[:100]}")

    return urls


def fetch_digest_page(digest_url):
    """抓取单期日报详情页，返回 (标题, 链接) 列表（仅招聘公告链接，排除导航/资讯链接）"""
    items = []

    try:
        resp = session.get(digest_url, timeout=20, verify=False)

        if resp.status_code != 200:
            print(f"    [{digest_url}] HTTP {resp.status_code}")
            return items

        resp.encoding = resp.apparent_encoding or "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")
        seen_urls = set()

        for a in soup.find_all(
            "a",
            href=re.compile(r"/announcement/detail/\d+\.html")
        ):
            title = a.get_text(strip=True)
            href = a["href"]

            if not title or len(title) < 6:
                continue

            full_url = normalize_gaoxiaojob_url(href)

            if not full_url or full_url in seen_urls:
                continue

            seen_urls.add(full_url)

            items.append({
                "title": title,
                "url": full_url
            })

        print(f"    [{digest_url}] 提取 {len(items)} 条公告链接")

    except Exception as e:
        print(f"    [{digest_url}] 失败: {str(e)[:100]}")

    return items


def fetch_gaoxiaojob_jobs():
    """抓取最近几期日报，按学科关键词筛选高校/科研岗位，按EHS关键词筛选企业岗位"""
    university_results = []
    corporate_results = []

    digest_urls = fetch_recent_digest_urls()

    for digest_url in digest_urls:
        items = fetch_digest_page(digest_url)
        time.sleep(1)

        for item in items:
            title = item["title"]
            url = item["url"]

            disc_hits = match_keywords(title, DISCIPLINE_KEYWORDS)

            if disc_hits:
                dedup_key = make_dedup_key("gaoxiaojob", url)

                university_results.append({
                    "category": "高校/科研",
                    "title": title,
                    "url": url,
                    "matched": "、".join(disc_hits),
                    "source": "高校人才网",
                    "dedup_key": dedup_key,
                })

                continue  # 一条公告只归入一类，避免重复统计

            ehs_hits = match_keywords(title, EHS_KEYWORDS)

            if ehs_hits:
                dedup_key = make_dedup_key("gaoxiaojob", url)

                corporate_results.append({
                    "category": "企业/EHS",
                    "title": title,
                    "url": url,
                    "matched": "、".join(ehs_hits),
                    "source": "高校人才网-企业招聘",
                    "dedup_key": dedup_key,
                })

    return university_results, corporate_results


# ==================== 数据源：百度/必应搜索（企业EHS补充，best-effort） ====================

def search_baidu(keyword):
    results = []

    try:
        import urllib.parse

        url = f"https://www.baidu.com/s?wd={urllib.parse.quote(keyword)}&rn=10"

        resp = session.get(url, timeout=15, verify=False)
        resp.encoding = resp.apparent_encoding or "utf-8"

        soup = BeautifulSoup(resp.text, "lxml")

        for item in soup.select(".result, .c-container"):
            title_tag = item.select_one("h3 a") or item.select_one("a")

            if not title_tag:
                continue

            title = title_tag.get_text(strip=True)
            href = title_tag.get("href", "")

            if title and href and match_keywords(title, EHS_KEYWORDS):
                dedup_key = make_dedup_key("baidu", href)

                results.append({
                    "category": "企业/EHS",
                    "title": title,
                    "url": href,
                    "matched": "、".join(
                        match_keywords(title, EHS_KEYWORDS)
                    ),
                    "source": "百度搜索",
                    "dedup_key": dedup_key,
                })

        print(f"  [百度:{keyword}] 筛选后 {len(results)} 条")

    except Exception as e:
        print(
            f"  [百度:{keyword}] 失败（常见于反爬限制）: "
            f"{str(e)[:80]}"
        )

    return results


def search_bing(keyword):
    results = []

    try:
        import urllib.parse

        url = (
            f"https://cn.bing.com/search?"
            f"q={urllib.parse.quote(keyword)}&count=10"
        )

        resp = session.get(url, timeout=15, verify=False)
        resp.encoding = resp.apparent_encoding or "utf-8"

        soup = BeautifulSoup(resp.text, "lxml")

        for item in soup.select("li.b_algo"):
            title_tag = item.select_one("h2 a")

            if not title_tag:
                continue

            title = title_tag.get_text(strip=True)
            href = title_tag.get("href", "")

            if title and href and match_keywords(title, EHS_KEYWORDS):
                dedup_key = make_dedup_key("bing", href)

                results.append({
                    "category": "企业/EHS",
                    "title": title,
                    "url": href,
                    "matched": "、".join(
                        match_keywords(title, EHS_KEYWORDS)
                    ),
                    "source": "必应搜索",
                    "dedup_key": dedup_key,
                })

        print(f"  [必应:{keyword}] 筛选后 {len(results)} 条")

    except Exception as e:
        print(
            f"  [必应:{keyword}] 失败（常见于反爬限制）: "
            f"{str(e)[:80]}"
        )

    return results


# ==================== 邮件通知 ====================

def send_email(subject, content):
    if not EMAIL_ENABLED:
        print("邮件未启用，仅打印：")
        print(f"  主题: {subject}")
        print(f"  内容:\n{content}")
        return

    # 支持多个收件邮箱，用英文逗号分隔，如 "a@qq.com,b@163.com"
    receivers = [
        r.strip()
        for r in RECEIVER.split(",")
        if r.strip()
    ]

    if not all([SENDER, PASSWORD]) or not receivers:
        print("邮件配置不完整，仅打印：")
        print(f"  主题: {subject}")
        print(f"  内容:\n{content}")
        return

    msg = MIMEText(content, "plain", "utf-8")
    msg["From"] = Header(SENDER)
    msg["To"] = Header(", ".join(receivers))
    msg["Subject"] = Header(subject, "utf-8")

    try:
        server = smtplib.SMTP_SSL(
            SMTP_SERVER,
            SMTP_PORT,
            timeout=30
        )

        server.login(SENDER, PASSWORD)

        server.sendmail(
            SENDER,
            receivers,
            msg.as_string()
        )

        server.quit()

        print(
            f"✅ 邮件发送成功（收件人: "
            f"{', '.join(receivers)}）"
        )

    except Exception as e:
        print(f"❌ 邮件发送失败: {e}")


# ==================== 主流程 ====================

def run_tracker():
    print(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"开始招聘信息追踪..."
    )

    history = load_history()

    print("正在抓取高校人才网每日汇总...")

    university_jobs, corporate_jobs_from_digest = (
        fetch_gaoxiaojob_jobs()
    )

    print(
        "正在搜索企业EHS/环境工程岗位"
        "（百度/必应，补充数据源）..."
    )

    corporate_jobs_from_search = []

    for kw in SEARCH_KEYWORDS:
        corporate_jobs_from_search.extend(
            search_baidu(kw)
        )

        time.sleep(2)

        corporate_jobs_from_search.extend(
            search_bing(kw)
        )

        time.sleep(2)

    all_results = (
        university_jobs
        + corporate_jobs_from_digest
        + corporate_jobs_from_search
    )

    # 去重
    seen_keys = set(
        history.get("seen_keys", [])
    )

    new_results = []

    for r in all_results:
        key = r["dedup_key"]

        if key not in seen_keys:
            seen_keys.add(key)
            history["seen_keys"].append(key)
            new_results.append(r)

    history["last_check"] = datetime.now().isoformat()

    save_history(history)

    new_university = [
        r for r in new_results
        if r["category"] == "高校/科研"
    ]

    new_corporate = [
        r for r in new_results
        if r["category"] == "企业/EHS"
    ]

    if new_results:
        total = len(new_results)
        batch_size = 20
        total_batches = (
            total + batch_size - 1
        ) // batch_size

        print(
            f"🎉 发现 {total} 条新招聘信息！"
            f"（高校/科研 {len(new_university)} 条，"
            f"企业/EHS {len(new_corporate)} 条）"
        )

        print(
            f"📧 将分成 {total_batches} 封邮件发送，"
            f"每封最多 {batch_size} 条。"
        )

        for batch_no, start in enumerate(
            range(0, total, batch_size),
            1
        ):
            batch = new_results[
                start:start + batch_size
            ]

            lines = [
                f"追踪时间: "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
                f"本邮件包含第 "
                f"{batch_no}/{total_batches} 批，"
                f"共 {len(batch)} 条信息。\n"
            ]

            batch_university = [
                r for r in batch
                if r["category"] == "高校/科研"
            ]

            batch_corporate = [
                r for r in batch
                if r["category"] == "企业/EHS"
            ]

            if batch_university:
                lines.append(
                    f"\n{'=' * 20} "
                    f"高校/科研岗位"
                    f"（{len(batch_university)}条） "
                    f"{'=' * 20}\n"
                )

                for i, r in enumerate(
                    batch_university,
                    1
                ):
                    lines.append(
                        f"{i}. {r['title']}\n"
                        f"   命中关键词: {r['matched']}\n"
                        f"   来源: {r['source']}\n"
                        f"   链接: {r['url']}\n"
                    )

            if batch_corporate:
                lines.append(
                    f"\n{'=' * 20} "
                    f"企业/EHS岗位"
                    f"（{len(batch_corporate)}条） "
                    f"{'=' * 20}\n"
                )

                for i, r in enumerate(
                    batch_corporate,
                    1
                ):
                    lines.append(
                        f"{i}. {r['title']}\n"
                        f"   命中关键词: {r['matched']}\n"
                        f"   来源: {r['source']}\n"
                        f"   链接: {r['url']}\n"
                    )

            content = "\n".join(lines)

            send_email(
                f"【招聘追踪】第 "
                f"{batch_no}/{total_batches} 封｜"
                f"共 {total} 条新信息",
                content,
            )

            if batch_no < total_batches:
                time.sleep(2)

    else:
        print("未发现新的相关招聘信息。")

    print("追踪结束。")


if __name__ == "__main__":
    run_tracker()
