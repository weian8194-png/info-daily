"""
信息聚合日报生成器
采集三大板块信息源 → 生成静态 HTML 站点 → 归档历史
用法: python generate.py
"""
import os
import sys
import json
import time
import hashlib
import shutil
import concurrent.futures
from datetime import datetime, timedelta, timezone, date
from dataclasses import dataclass, field, asdict
from collections import defaultdict
from typing import Optional

import requests
import feedparser
from bs4 import BeautifulSoup

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, 'output')

# 北京时间
TZ = timezone(timedelta(hours=8))
TODAY = datetime.now(TZ).strftime('%Y-%m-%d')
TODAY_DATE = datetime.now(TZ).date()
NOW = datetime.now(TZ)

# 配置
MAX_AGE_HOURS = 48        # 只保留近48小时的内容
MAX_ITEMS_PER_SECTION = {
    'policy': 12,
    'trend': 8,
    'ai': 20,
}

# 请求头（模拟浏览器避免被ban）
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
}

# === 数据模型 ===

@dataclass
class Item:
    title: str
    url: str
    summary: str          # 总结（1-2句核心信息）
    background: str = ''  # 背景（为什么重要、前因后果）
    source: str = ''      # 来源标识
    source_name: str = '' # 显示名称
    category: str = ''    # policy / trend / ai
    date: str = ''        # YYYY-MM-DD
    rank: int = 0         # 排序权重（越小越靠前）
    pub_date: datetime = None  # 实际发布时间（用于日期过滤）

# === 工具函数 ===

def _get(url: str, timeout: int = 15, headers: dict = None) -> Optional[bytes]:
    """安全 HTTP GET，返回 bytes 交由 BeautifulSoup 自动识别编码"""
    try:
        resp = requests.get(url, headers=headers or HEADERS, timeout=timeout)
        resp.raise_for_status()
        return resp.content
    except Exception:
        return None

def _fetch_rss(url: str, timeout: int = 15) -> list:
    """获取 RSS/Atom feed，返回 feedparser 条目列表"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        return feed.entries
    except Exception:
        return []

def _truncate(text: str, max_len: int = 120) -> str:
    """截断文本"""
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len-3] + '...'

def _clean_html(html: str) -> str:
    """去除 HTML 标签，保留纯文本"""
    try:
        soup = BeautifulSoup(html, 'html.parser')
        return soup.get_text(' ', strip=True)
    except Exception:
        return html

def _item_hash(item: Item) -> str:
    """基于 URL 的去重 hash"""
    return hashlib.md5(item.url.encode()).hexdigest()[:12]

def _now_str() -> str:
    return datetime.now(TZ).strftime('%Y-%m-%d %H:%M')

# ============================================================
#  板块1: 跨境电商平台政策
# ============================================================

def fetch_ecommerce_news() -> list:
    """采集跨境电商平台政策 — 只保留真正的政策/规则变更类内容"""
    items = []

    # === 政策精准关键词 ===
    # 每个关键词对应 Google News RSS 搜索
    policy_queries = [
        # 亚马逊政策类
        'Amazon+ seller+ policy+ change+ fee',
        'Amazon+ FBA+ policy+ update+ requirement',
        'Amazon+ return+ policy+ compliance+ restriction',
        # TEMU 政策类
        'TEMU+ merchant+ policy+ commission+ rule',
        # TikTok Shop 政策类
        'TikTok+ Shop+ seller+ policy+ update',
        # 跨境电商政策（中文）
        '跨境电商+ 平台+ 政策+ 新规+ 亚马逊',
        # 关税/合规类（影响跨境卖家）
        'US+ tariff+ China+ ecommerce+ de+ minimis',
    ]
    # 反关键词：标题含以下词的直接排除（非政策类营销软文/无关内容）
    anti_keywords = [
        '选品', '爆款', '测评', '对比', 'vs', 'VS', '干货', '技巧',
        '教程', '引流', '打法', '黑科技', '秘籍', '日出', '万单',
        '纯利', '暴利', 'Top', 'review', 'rating', 'best',
        'Guide', 'guide', 'How to', 'Tips', 'tutorial', 'AWS', 'Config',
        'stock', 'earnings', 'revenue', 'profit', 'dividend',  # 排除股票财报
    ]

    for query in policy_queries:
        url = f'https://news.google.com/rss/search?q={query}&hl=zh-CN&gl=US&ceid=US:en'
        entries = _fetch_rss(url)
        for e in entries[:8]:
            title = e.get('title', '')
            link = e.get('link', '')
            pub = e.get('published', '')
            pub_dt = _parse_date(pub)

            # 获取完整 RSS 描述（不截断，后续做深度摘要）
            raw_desc = _clean_html(e.get('summary', e.get('description', '')))
            if not raw_desc:
                continue

            # 标题清洗
            clean_title = title.split(' - ')[0].strip()
            source_part = title.split(' - ')[-1].strip() if ' - ' in title else ''

            # 反关键词过滤
            combined = f'{clean_title} {raw_desc[:100]}'.lower()
            if any(ak.lower() in combined for ak in anti_keywords):
                continue

            # 深度总结：取 RSS 描述的前 400 字符（通常涵盖新闻核心）
            deep_summary = raw_desc[:500].strip()
            if len(deep_summary) < 50:
                deep_summary = raw_desc[:200].strip()

            # 背景：补充来源和时间信息
            bg_parts = [f'来源：{source_part}' if source_part else 'Google News 政策监测']
            if pub_dt:
                bg_parts.append(f'发布时间：{pub_dt.strftime("%Y-%m-%d %H:%M")}')
            bg_parts.append(f'监测关键词：{query.replace("+", " ")}')

            items.append(Item(
                title=clean_title,
                url=link,
                summary=deep_summary,
                background=' | '.join(bg_parts),
                source='google-news',
                source_name='Google News',
                category='policy',
                date=pub_dt.strftime('%Y-%m-%d') if pub_dt else TODAY,
                pub_date=pub_dt,
            ))

    # === 雨果网政策类文章（辅助源） ===
    try:
        html = _get('https://www.cifnews.com/', timeout=10)
        if html:
            soup = BeautifulSoup(html, 'html.parser')
            for a in soup.select('a[href]'):
                href = a.get('href', '').strip()
                title = a.get_text(strip=True)
                is_article = ('/article/' in href or '/news/' in href)
                if not is_article or not title or len(title) < 15:
                    continue
                # 反关键词过滤
                if any(ak.lower() in title.lower() for ak in anti_keywords):
                    continue
                # 政策类正关键词：标题必须包含至少一个
                policy_kw = ['政策', '新规', '关税', '合规', 'FBA', '仓储', '退货',
                            '佣金', '入驻', '封号', '验证', '税务', 'VAT', 'FDA',
                            'CPSC', '认证', '限仓', '费用', '调整', '变更', '生效',
                            'USPS', '物流', '海关', '清关', 'TRO', '侵权', '专利']
                if not any(pk in title for pk in policy_kw):
                    continue
                if not href.startswith('http'):
                    href = 'https://www.cifnews.com' + href
                items.append(Item(
                    title=title,
                    url=href,
                    summary=f'雨果网政策资讯：{_truncate(title, 200)}。点击链接查看政策全文与解读。',
                    background='雨果网（cifnews.com）跨境电商行业媒体 — 政策/法规/合规类资讯',
                    source='cifnews',
                    source_name='雨果网',
                    category='policy',
                    date=TODAY,
                    pub_date=NOW,
                ))
    except Exception:
        pass

    return _dedup_sort(items, max_items=MAX_ITEMS_PER_SECTION['policy'])

# ============================================================
#  板块2: 小家电产品趋势
# ============================================================

def fetch_appliance_trends() -> list:
    """采集小家电产品趋势"""
    items = []

    # Google Trends — 小家电关键词（用户提供的数据源参考）
    try:
        from pytrends.request import TrendReq
        pytrends = TrendReq(hl='zh-CN', tz=480, timeout=10)
        # 高价位小家电 + 新品类趋势
        kw_list = [
            'air fryer', 'mini fridge', 'coffee maker', 'blender', 'robot vacuum',
            'portable fan', 'dehumidifier', 'yogurt maker',
            'premium kitchen appliance', 'smart coffee maker', 'portable blender',
            'ice maker', 'soda maker', 'electric kettle', 'toaster oven',
            'food processor', 'slow cooker', 'air purifier', 'handheld vacuum',
        ]
        pytrends.build_payload(kw_list, timeframe='now 7-d', geo='US')
        related = pytrends.related_queries()
        for kw in kw_list:
            rising = related.get(kw, {}).get('rising', None)
            if rising is not None and not rising.empty:
                for _, row in rising.head(3).iterrows():
                    query = row.get('query', '')
                    pct = row.get('value', 0)
                    if query:
                        items.append(Item(
                            title=f'"{query}" 搜索量上升',
                            url=f'https://trends.google.com/trends/explore?q={query}&geo=US',
                            summary=f'美国市场小家电关键词「{query}」近7天搜索热度攀升 {pct}%，关联品类「{kw}」',
                            background=f'Google Trends 美国区数据，品类：{kw}，时间范围：7天。可用于判断产品生命周期和市场热度变化',
                            source='google-trends',
                            source_name='Google Trends',
                            category='trend',
                            date=TODAY,
                            pub_date=NOW,
                            rank=100 - int(pct),
                        ))
    except Exception:
        pass

    # Product Hunt — 硬件/小家电品类
    try:
        html = _get('https://www.producthunt.com/leaderboard/daily/' +
                    TODAY_DATE.strftime('%Y/%-m/%-d'), timeout=10)
        if html:
            soup = BeautifulSoup(html, 'html.parser')
            for item_el in soup.select('[data-test="product-item"]')[:10]:
                name_el = item_el.select_one('[data-test="product-name"]')
                tag_el = item_el.select_one('[data-test="product-tagline"]')
                link_el = item_el.select_one('a[href^="/posts/"]')
                if name_el:
                    name = name_el.get_text(strip=True)
                    tagline = tag_el.get_text(strip=True) if tag_el else ''
                    href = 'https://www.producthunt.com' + link_el['href'] if link_el else ''
                    hw_keywords = ['hardware', 'device', 'iot', 'kitchen', 'home',
                                   'appliance', 'gadget', 'coffee', 'cook']
                    combined = (name + ' ' + tagline).lower()
                    if any(kw in combined for kw in hw_keywords) and href:
                        items.append(Item(
                            title=f'{name} — {tagline}' if tagline else name,
                            url=href,
                            summary=f'Product Hunt 热门硬件新品: {_truncate(tagline, 120)}',
                            background=f'Product Hunt 每日排行榜硬件/小家电品类新品，反映欧美市场消费电子趋势',
                            source='producthunt',
                            source_name='Product Hunt',
                            category='trend',
                            date=TODAY,
                            pub_date=NOW,
                        ))
    except Exception:
        pass

    # 如果来源为空，加占位项引导手动查看
    if not items:
        items.append(Item(
            title='今日自动数据较少，建议手动查看',
            url='https://trends.google.com/trends/explore?geo=US&q=kitchen%20appliance,coffee%20maker,air%20fryer',
            summary='Google Trends 自动采集未获取到足够数据，可点击链接手动查看小家电品类趋势',
            background='建议关注的趋势关键词：premium kitchen appliance, smart coffee maker, portable blender, ice maker, soda maker, electric kettle, air purifier。也可查看 Amazon Best Sellers Kitchen 品类、TikTok Creative Center 热门标签',
            source='placeholder',
            source_name='手动',
            category='trend',
            date=TODAY,
            pub_date=NOW,
            rank=99,
        ))

    return _dedup_sort(items)

# ============================================================
#  板块3: AI & Vibe Coding
# ============================================================

def fetch_hackernews() -> list:
    """Hacker News — AI/vibe coding 相关"""
    items = []
    queries = ['AI', 'vibe%20coding', 'LLM', 'Claude', 'Cursor', 'agent']
    for q in queries:
        url = f'https://hnrss.org/frontpage?q={q}&count=5'
        entries = _fetch_rss(url)
        for e in entries:
            title = e.get('title', '')
            link = e.get('link', '')
            desc = _clean_html(e.get('description', '')[:400])
            pub = e.get('published', '')
            pub_dt = _parse_date(pub)
            if title and link:
                comments = e.get('comments', '')
                bg = f'HN 社区讨论帖'
                if comments:
                    bg += f'，[查看HN评论]({comments})'
                items.append(Item(
                    title=title, url=link,
                    summary=_truncate(desc, 200),
                    background=bg,
                    source='hackernews', source_name='HN',
                    category='ai',
                    date=pub_dt.strftime('%Y-%m-%d') if pub_dt else TODAY,
                    pub_date=pub_dt,
                ))
    return items

def fetch_github_trending() -> list:
    """GitHub Trending — 每日热门仓库"""
    items = []
    html = _get('https://github.com/trending?since=daily', timeout=15)
    if not html:
        return items
    try:
        soup = BeautifulSoup(html, 'html.parser')
        for article in soup.select('article.Box-row')[:15]:
            h2 = article.select_one('h2 a')
            if not h2: continue
            repo = h2.get('href', '').strip()
            name = ' '.join(h2.stripped_strings).replace('\n', '').strip()
            desc_el = article.select_one('p')
            desc = desc_el.get_text(strip=True) if desc_el else ''
            lang_el = article.select_one('[itemprop="programmingLanguage"]')
            lang = lang_el.get_text(strip=True) if lang_el else ''
            stars_el = article.select_one('.d-inline-block.float-sm-right')
            stars = stars_el.get_text(strip=True) if stars_el else ''
            ai_kw = ['ai', 'llm', 'gpt', 'claude', 'cursor', 'vibe', 'agent',
                     'copilot', 'ml', 'machine-learning', 'deep-learning',
                     'langchain', 'openai', 'anthropic', 'diffusion', 'transformer',
                     'chat', 'prompt', 'rag', 'embedding', 'whisper', 'stable-diffusion',
                     'coder', 'mcp', 'tool', 'sdk', 'framework']
            repo_lower = (name + ' ' + desc).lower()
            if any(kw in repo_lower for kw in ai_kw):
                items.append(Item(
                    title=name, url=f'https://github.com{repo}',
                    summary=_truncate(f'[{lang}] {desc}' if lang else desc, 180),
                    background=f'GitHub Trending 今日热门仓库（{stars}），语言：{lang or "未知"}',
                    source='github', source_name='GitHub',
                    category='ai', date=TODAY, pub_date=NOW,
                ))
    except Exception:
        pass
    return items

def fetch_producthunt() -> list:
    """Product Hunt — AI 相关产品"""
    items = []
    entries = _fetch_rss('https://www.producthunt.com/feed', timeout=15)
    ai_kw = ['AI', 'GPT', 'Claude', 'LLM', 'agent', 'copilot', 'automation',
             'developer', 'code', 'dev', 'api', 'nocode', 'lowcode', 'vibe']
    for e in entries[:30]:
        title = e.get('title', '')
        link = e.get('link', '')
        desc = _clean_html(e.get('summary', '')[:350])
        tags = ' '.join([t.get('term', '') for t in e.get('tags', [])])
        combined = f'{title} {desc} {tags}'.lower()
        if any(kw.lower() in combined for kw in ai_kw):
            tag_list = [t.get('term', '') for t in e.get('tags', []) if t.get('term')]
            items.append(Item(
                title=title, url=link,
                summary=_truncate(desc, 180),
                background=f'Product Hunt 今日热门产品，标签：{", ".join(tag_list[:5]) if tag_list else "AI/工具"}',
                source='producthunt', source_name='Product Hunt',
                category='ai', date=TODAY, pub_date=NOW,
            ))
    return items[:15]

def fetch_juejin() -> list:
    """掘金 RSS"""
    items = []
    entries = _fetch_rss('https://juejin.cn/rss', timeout=10)
    ai_kw = ['AI', 'LLM', 'GPT', 'Claude', 'Cursor', '大模型', '人工智能',
             'Agent', 'Copilot', 'Vibe', '编程', '工具', '开源', '前端']
    for e in entries[:40]:
        title = e.get('title', '')
        link = e.get('link', '')
        desc = _clean_html(e.get('description', '')[:300])
        pub = e.get('published', '')
        pub_dt = _parse_date(pub)
        combined = f'{title} {desc}'
        if any(kw in combined for kw in ai_kw):
            items.append(Item(
                title=title, url=link,
                summary=_truncate(desc, 180),
                background='掘金开发者社区热门文章',
                source='juejin', source_name='掘金',
                category='ai',
                date=pub_dt.strftime('%Y-%m-%d') if pub_dt else TODAY,
                pub_date=pub_dt,
            ))
    return items[:12]

def fetch_infoq() -> list:
    """InfoQ 中国 RSS"""
    items = []
    entries = _fetch_rss('https://www.infoq.cn/feed', timeout=10)
    for e in entries[:15]:
        title = e.get('title', '')
        link = e.get('link', '')
        desc = _clean_html(e.get('summary', '')[:300])
        pub = e.get('published', '')
        pub_dt = _parse_date(pub)
        if title and link:
            items.append(Item(
                title=title, url=link,
                summary=_truncate(desc, 180) if desc else 'InfoQ 技术资讯',
                background='InfoQ 中国专业技术媒体，关注前沿技术动态与架构实践',
                source='infoq', source_name='InfoQ',
                category='ai',
                date=pub_dt.strftime('%Y-%m-%d') if pub_dt else TODAY,
                pub_date=pub_dt,
            ))
    return items

# ============================================================
#  采集编排
# ============================================================

def _parse_date(date_str: str) -> Optional[datetime]:
    """解析日期字符串 → datetime 对象（带时区）"""
    if not date_str:
        return None
    try:
        from time import mktime
        parsed = feedparser._parse_date(date_str)
        if parsed:
            return datetime.fromtimestamp(mktime(parsed), tz=TZ)
    except Exception:
        pass
    return None

def _dedup_sort(items: list, max_items: int = 20) -> list:
    """URL 去重 + 日期过滤 + 排名排序"""
    # 日期过滤
    cutoff = NOW - timedelta(hours=MAX_AGE_HOURS)
    recent = []
    for item in items:
        if item.pub_date is None:
            # 无日期信息的保留（不会太多，通常来自抓取）
            recent.append(item)
        elif item.pub_date >= cutoff:
            recent.append(item)
        # 太旧的丢弃

    # URL 去重
    seen = set()
    unique = []
    for item in recent:
        h = _item_hash(item)
        if h not in seen:
            seen.add(h)
            unique.append(item)

    unique.sort(key=lambda x: (x.rank, x.title))
    return unique[:max_items]

def collect_all() -> dict:
    """并行采集所有信息源，返回分类好的数据"""
    results = {'policy': [], 'trend': [], 'ai': []}
    all_ai = []

    # 定义任务
    tasks = {
        'policy': [('跨境电商资讯', fetch_ecommerce_news)],
        'trend':  [('小家电趋势', fetch_appliance_trends)],
        'ai': [
            ('Hacker News', fetch_hackernews),
            ('GitHub Trending', fetch_github_trending),
            ('Product Hunt', fetch_producthunt),
            ('掘金', fetch_juejin),
            ('InfoQ', fetch_infoq),
        ],
    }

    # 顺序执行（避免并行导致限流问题），每个源独立容错
    for cat, funcs in tasks.items():
        for name, func in funcs:
            try:
                print(f'  [采集] {name} ...')
                items = func()
                if items:
                    print(f'         → {len(items)} 条')
                else:
                    print(f'         → 0 条（源可能不可用）')
                if cat == 'ai':
                    all_ai.extend(items)
                else:
                    results[cat].extend(items)
            except Exception as e:
                print(f'         → 出错: {e}')

    # AI板块去重排序
    results['ai'] = _dedup_sort(all_ai, max_items=MAX_ITEMS_PER_SECTION['ai'])

    # policy 和 trend 也去重
    results['policy'] = _dedup_sort(results['policy'], max_items=MAX_ITEMS_PER_SECTION['policy'])
    results['trend'] = _dedup_sort(results['trend'], max_items=MAX_ITEMS_PER_SECTION['trend'])

    return results

# ============================================================
#  HTML 生成
# ============================================================

def _source_tag(source_name: str) -> str:
    """来源名 → CSS class"""
    m = {
        'HN': 'tag-hn', 'GitHub': 'tag-gh', 'Product Hunt': 'tag-ph',
        '掘金': 'tag-jj', 'InfoQ': 'tag-iq', 'Google Trends': 'tag-gt',
        'Google News': 'tag-gn', '雨果网': 'tag-cn', '手动': 'tag-manual',
    }
    return m.get(source_name, 'tag-default')

def _render_section(title: str, icon: str, items: list) -> str:
    """渲染一个内容板块（卡片格式：总结 + 背景 + 链接）"""
    if not items:
        return f'''<div class="section">
    <h2>{icon} {title}</h2>
    <div class="empty">今日暂无数据，请稍后刷新</div>
</div>'''

    cards = []
    for item in items:
        tag_cls = _source_tag(item.source_name)
        time_html = f'<span class="time">{item.date}</span>' if item.date else ''
        bg_html = ''
        if item.background:
            bg_html = f'<div class="card-bg"><span class="bg-label">背景</span>{item.background}</div>'

        cards.append(f'''<div class="card-item">
    <div class="card-header">
        <span class="source-tag {tag_cls}">{item.source_name}</span>
        <a href="{item.url}" target="_blank" rel="noopener" class="card-title">{item.title}</a>
        {time_html}
    </div>
    <div class="card-summary"><span class="section-label">总结</span>{item.summary}</div>
    {bg_html}
    <div class="card-link"><span class="section-label">链接</span><a href="{item.url}" target="_blank" rel="noopener">{item.url[:80]}{'...' if len(item.url) > 80 else ''}</a></div>
</div>''')

    return f'''<div class="section">
    <h2>{icon} {title} <span class="count">({len(items)}条)</span></h2>
    {''.join(cards)}
</div>'''

def render_html(data: dict, gen_date: str, gen_time: str) -> str:
    """生成完整 HTML 页面"""
    policy_count = len(data['policy'])
    trend_count = len(data['trend'])
    ai_count = len(data['ai'])
    total = policy_count + trend_count + ai_count

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>跨境 & AI 信息日报 — {gen_date}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Microsoft YaHei", sans-serif; background: #f5f6fa; color: #333; line-height:1.6; }}
.header {{ background: linear-gradient(135deg, #0f2027 0%, #203a43 50%, #2c5364 100%); color:#fff; padding:32px 24px; text-align:center; }}
.header h1 {{ font-size:26px; margin-bottom:6px; letter-spacing:1px; }}
.header .meta {{ font-size:13px; opacity:.75; }}
.header .meta a {{ color:#8ecae6; text-decoration:none; }}
.nav {{ background:#fff; border-bottom:1px solid #e0e0e0; padding:0 24px; display:flex; gap:0; justify-content:center; }}
.nav a {{ padding:10px 20px; text-decoration:none; color:#555; font-size:14px; font-weight:500; border-bottom:2px solid transparent; transition:.2s; }}
.nav a:hover, .nav a.active {{ color:#2c5364; border-bottom-color:#2c5364; }}
.container {{ max-width:900px; margin:0 auto; padding:20px; }}
.section {{ background:#fff; border-radius:10px; padding:24px; margin-bottom:20px; box-shadow:0 1px 3px rgba(0,0,0,0.06); }}
.section h2 {{ font-size:18px; margin-bottom:16px; padding-bottom:10px; border-bottom:2px solid #2c5364; }}
.section h2 .count {{ font-size:13px; color:#999; font-weight:400; margin-left:8px; }}
.source-tag {{ flex-shrink:0; padding:3px 10px; border-radius:4px; font-size:11px; font-weight:600; white-space:nowrap; }}
.tag-hn {{ background:#ff6600; color:#fff; }}
.tag-gh {{ background:#24292e; color:#fff; }}
.tag-ph {{ background:#da552f; color:#fff; }}
.tag-jj {{ background:#1e80ff; color:#fff; }}
.tag-iq {{ background:#009a61; color:#fff; }}
.tag-gt {{ background:#4285f4; color:#fff; }}
.tag-gn {{ background:#ea4335; color:#fff; }}
.tag-cn {{ background:#ff6b35; color:#fff; }}
.tag-manual {{ background:#999; color:#fff; }}
.tag-default {{ background:#e0e0e0; color:#555; }}
.time {{ color:#bbb; font-size:11px; white-space:nowrap; flex-shrink:0; margin-left:auto; }}
.empty {{ color:#999; font-size:13px; text-align:center; padding:20px; }}
.footer {{ text-align:center; color:#bbb; font-size:12px; padding:30px; }}
.footer a {{ color:#999; text-decoration:none; }}
.stats {{ display:flex; gap:20px; justify-content:center; flex-wrap:wrap; margin-bottom:24px; }}
.stat-card {{ background:#fff; border-radius:10px; padding:16px 24px; text-align:center; box-shadow:0 1px 3px rgba(0,0,0,0.06); min-width:100px; }}
.stat-card .num {{ font-size:28px; font-weight:700; color:#2c5364; }}
.stat-card .label {{ font-size:12px; color:#999; margin-top:2px; }}

/* 卡片格式 */
.card-item {{ background:#fafbfc; border-radius:8px; padding:16px 20px; margin-bottom:14px; border:1px solid #eef0f4; transition:.2s; }}
.card-item:hover {{ border-color:#c0c8d4; box-shadow:0 2px 8px rgba(0,0,0,0.04); }}
.card-header {{ display:flex; align-items:center; gap:10px; margin-bottom:10px; flex-wrap:wrap; }}
.card-title {{ color:#2c5364; text-decoration:none; font-weight:600; font-size:15px; flex:1; min-width:200px; }}
.card-title:hover {{ text-decoration:underline; color:#1a73e8; }}
.card-summary {{ font-size:13px; color:#555; line-height:1.6; margin-bottom:8px; padding:10px 14px; background:#f0f4ff; border-radius:6px; border-left:3px solid #2c5364; }}
.card-bg {{ font-size:12px; color:#666; line-height:1.5; margin-bottom:8px; padding:8px 14px; background:#fffbe6; border-radius:6px; }}
.card-link {{ font-size:12px; color:#888; padding:6px 14px; background:#f8f9fa; border-radius:6px; word-break:break-all; }}
.card-link a {{ color:#2c5364; text-decoration:none; }}
.card-link a:hover {{ text-decoration:underline; }}
.section-label {{ display:inline-block; font-size:10px; font-weight:700; color:#fff; background:#2c5364; padding:1px 6px; border-radius:3px; margin-right:8px; text-transform:uppercase; letter-spacing:.5px; }}
.bg-label {{ display:inline-block; font-size:10px; font-weight:700; color:#b7950b; background:#fff3cd; padding:1px 6px; border-radius:3px; margin-right:8px; }}

@media (max-width: 640px) {{
    .header h1 {{ font-size:20px; }}
    .container {{ padding:10px; }}
    .section {{ padding:16px; }}
    .card-item {{ padding:12px 14px; }}
    .card-title {{ font-size:13px; min-width:0; }}
    .stats {{ gap:10px; }}
    .stat-card {{ padding:12px 16px; min-width:80px; }}
    .stat-card .num {{ font-size:22px; }}
}}
</style>
</head>
<body>

<div class="header">
    <h1>跨境 & AI 信息日报</h1>
    <div class="meta">
        {gen_date} · 每日 9:00 更新 · 共 {total} 条信息
    </div>
</div>

<div class="nav">
    <a href="index.html" class="active">今日</a>
    <a href="archive.html">历史归档</a>
</div>

<div class="container">

    <div class="stats">
        <div class="stat-card"><div class="num">{policy_count}</div><div class="label">平台政策</div></div>
        <div class="stat-card"><div class="num">{trend_count}</div><div class="label">产品趋势</div></div>
        <div class="stat-card"><div class="num">{ai_count}</div><div class="label">AI 动态</div></div>
    </div>

{_render_section('平台政策', '📋', data['policy'])}
{_render_section('小家电趋势', '📈', data['trend'])}
{_render_section('AI & Vibe Coding', '🤖', data['ai'])}

</div>

<div class="footer">
    <p>自动采集生成于 {gen_time} · 数据来源: Hacker News / GitHub / Product Hunt / 掘金 / InfoQ / Google News / Google Trends</p>
    <p style="margin-top:6px;"><a href="archive.html">查看历史日报</a> · 内容仅供参考</p>
</div>

</body></html>'''

# ============================================================
#  归档管理
# ============================================================

def _archive_dir(d: date) -> str:
    """获取归档目录路径"""
    return os.path.join(OUTPUT_DIR, str(d.year), f'{d.month:02d}', f'{d.day:02d}')

def save_and_archive(html: str, gen_date: date):
    """保存首页 + 日期归档"""
    # 首页
    index_path = os.path.join(OUTPUT_DIR, 'index.html')
    with open(index_path, 'w', encoding='utf-8') as f:
        f.write(html)

    # 日期归档目录
    archive_path = _archive_dir(gen_date)
    os.makedirs(archive_path, exist_ok=True)
    archive_file = os.path.join(archive_path, 'index.html')
    with open(archive_file, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f'  [保存] 首页: {index_path}')
    print(f'  [归档] 日期: {archive_file}')

def update_archive_index():
    """更新历史归档索引页 archive.html"""
    # 扫描所有已有归档日期
    dates = set()
    if os.path.exists(OUTPUT_DIR):
        for root, dirs, files in os.walk(OUTPUT_DIR):
            if 'index.html' in files:
                rel = os.path.relpath(root, OUTPUT_DIR)
                parts = rel.replace('\\', '/').split('/')
                if len(parts) == 3 and all(p.isdigit() for p in parts):
                    try:
                        d = date(int(parts[0]), int(parts[1]), int(parts[2]))
                        dates.add(d)
                    except ValueError:
                        pass

    sorted_dates = sorted(dates, reverse=True)
    if not sorted_dates:
        return

    rows = []
    for d in sorted_dates:
        archive_url = f'{d.year}/{d.month:02d}/{d.day:02d}/'
        weekday = ['周一','周二','周三','周四','周五','周六','周日'][d.weekday()]
        rows.append(f'<li><a href="{archive_url}">{d.strftime("%Y-%m-%d")}</a> <span class="wd">{weekday}</span></li>')

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>历史归档 — 信息日报</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Microsoft YaHei", sans-serif; background:#f5f6fa; color:#333; }}
.header {{ background: linear-gradient(135deg, #0f2027, #203a43, #2c5364); color:#fff; padding:28px 24px; text-align:center; }}
.header h1 {{ font-size:24px; margin-bottom:4px; }}
.header .meta {{ font-size:13px; opacity:.75; }}
.nav {{ background:#fff; border-bottom:1px solid #e0e0e0; padding:0 24px; display:flex; gap:0; justify-content:center; }}
.nav a {{ padding:10px 20px; text-decoration:none; color:#555; font-size:14px; font-weight:500; border-bottom:2px solid transparent; }}
.nav a:hover, .nav a.active {{ color:#2c5364; border-bottom-color:#2c5364; }}
.container {{ max-width:700px; margin:0 auto; padding:24px; }}
.section {{ background:#fff; border-radius:10px; padding:24px; box-shadow:0 1px 3px rgba(0,0,0,0.06); }}
.section h2 {{ font-size:18px; margin-bottom:16px; }}
.archive-list {{ list-style:none; }}
.archive-list li {{ padding:10px 0; border-bottom:1px solid #f0f0f0; display:flex; align-items:center; gap:10px; }}
.archive-list li a {{ color:#2c5364; text-decoration:none; font-weight:500; font-size:15px; }}
.archive-list li a:hover {{ text-decoration:underline; }}
.archive-list .wd {{ color:#bbb; font-size:12px; }}
.count {{ color:#999; font-size:12px; margin-left:auto; }}
.footer {{ text-align:center; color:#bbb; font-size:12px; padding:30px; }}
.footer a {{ color:#999; text-decoration:none; }}
@media (max-width: 640px) {{
    .container {{ padding:10px; }}
}}
</style>
</head>
<body>
<div class="header">
    <h1>历史归档</h1>
    <div class="meta">{len(sorted_dates)} 天记录</div>
</div>
<div class="nav">
    <a href="index.html">今日</a>
    <a href="archive.html" class="active">历史归档</a>
</div>
<div class="container">
    <div class="section">
        <h2>日报列表</h2>
        <ul class="archive-list">
            {''.join(rows)}
        </ul>
    </div>
</div>
<div class="footer">
    <p><a href="index.html">返回今日日报</a></p>
</div>
</body></html>'''

    archive_path = os.path.join(OUTPUT_DIR, 'archive.html')
    with open(archive_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'  [索引] 归档页: {archive_path} ({len(sorted_dates)} 天)')

# ============================================================
#  GitHub Actions 工作流
# ============================================================

def ensure_github_actions():
    """创建 GitHub Actions 工作流文件"""
    actions_dir = os.path.join(BASE_DIR, '.github', 'workflows')
    os.makedirs(actions_dir, exist_ok=True)
    workflow_path = os.path.join(actions_dir, 'daily.yml')

    if os.path.exists(workflow_path):
        return

    workflow = '''name: Daily Info Digest
on:
  schedule:
    - cron: '0 1 * * *'   # UTC 1:00 = 北京时间 9:00
  workflow_dispatch:       # 支持手动触发

jobs:
  generate:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: Install dependencies
        run: pip install requests feedparser beautifulsoup4 pytrends jinja2
      - name: Generate digests
        run: python generate.py
      - name: Deploy to GitHub Pages
        uses: peaceiris/actions-gh-pages@v4
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          publish_dir: ./output
          enable_jekyll: false
          keep_files: true
'''
    with open(workflow_path, 'w', encoding='utf-8') as f:
        f.write(workflow)
    print(f'  [Actions] 已创建: {workflow_path}')

# ============================================================
#  主入口
# ============================================================

def main():
    gen_time = _now_str()
    gen_date = TODAY_DATE
    print('=' * 50)
    print(f'  信息聚合日报生成器')
    print(f'  日期: {gen_date}  时间: {gen_time}')
    print('=' * 50)
    print()

    # 1. 采集
    print('[1/4] 采集信息源...')
    data = collect_all()
    total = sum(len(v) for v in data.values())
    print(f'  总计采集 {total} 条 (政策{len(data["policy"])} + 趋势{len(data["trend"])} + AI{len(data["ai"])})')
    print()

    # 2. 生成 HTML
    print('[2/4] 生成 HTML...')
    html = render_html(data, gen_date.strftime('%Y-%m-%d'), gen_time)
    print(f'  HTML 大小: {len(html)} 字符')
    print()

    # 3. 保存 + 归档
    print('[3/4] 保存与归档...')
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    save_and_archive(html, gen_date)
    update_archive_index()
    print()

    # 4. GitHub Actions
    print('[4/4] 检查 GitHub Actions...')
    ensure_github_actions()

    print()
    print('=' * 50)
    print(f'  完成！打开 output/index.html 查看日报')
    print(f'  部署到 GitHub Pages: 推送仓库后自动部署')
    print('=' * 50)

if __name__ == '__main__':
    main()
