import feedparser
import requests
from bs4 import BeautifulSoup
import hashlib
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
from urllib.parse import urljoin, urlsplit
from concurrent.futures import ThreadPoolExecutor, as_completed
from ...core.config.config import settings
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class NewsCollector:

    def __init__(self):
        self.news_sources = settings.resolved_news_sources()
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        }
        self.proxies = None
        if settings.http_proxy:
            self.proxies = {
                'http': settings.http_proxy,
                'https': settings.http_proxy,
            }
            logger.info("RSS proxy is configured")

    @staticmethod
    def _is_external_link(url: str) -> bool:
        return urlsplit(str(url or '')).scheme.lower() in {'http', 'https'}

    def _request(self, url: str, source: Dict[str, Any], timeout: int):
        request_kwargs = {
            'headers': self.headers,
            'timeout': timeout,
            'allow_redirects': True,
        }
        if source.get('proxy_mode') == 'bypass':
            session = requests.Session()
            session.trust_env = False
            try:
                return session.get(url, **request_kwargs)
            finally:
                session.close()
        return requests.get(url, proxies=self.proxies, **request_kwargs)

    def collect_news(self) -> List[Dict[str, Any]]:
        all_news = []
        enabled_sources = [s for s in self.news_sources if s.get('enabled', False)]

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {}
            for source in enabled_sources:
                mode = source.get('mode', 'rss')
                if mode == 'scrape':
                    future = executor.submit(self._collect_by_scraping, source)
                else:
                    future = executor.submit(self._collect_by_rss, source)
                futures[future] = source

            for future in as_completed(futures):
                source = futures[future]
                try:
                    news = future.result()
                    all_news.extend(news)
                    logger.info(f"成功采集 {source['name']} 的 {len(news)} 条新闻")
                except Exception as e:
                    logger.error(f"采集 {source['name']} 时出错: {str(e)}")

        unique_news = self._deduplicate_news(all_news)
        logger.info(f"去重后剩余 {len(unique_news)} 条新闻")
        return unique_news

    def _collect_by_rss(self, source: Dict[str, Any]) -> List[Dict[str, Any]]:
        response = self._request(source['url'], source, timeout=20)
        if response.status_code != 200:
            raise RuntimeError(f"RSS 请求失败: HTTP {response.status_code}")

        feed = feedparser.parse(response.content)
        entries = list(feed.entries or [])
        if feed.bozo:
            message = f"解析 {source['name']} 的 RSS 时出错: {feed.bozo_exception}"
            if not entries:
                raise ValueError(message)
            logger.warning(message)
        if not entries:
            logger.warning(f"{source['name']} RSS 返回空 Feed")
            return []

        news_list = []
        skipped_invalid = 0
        skipped_keyword = 0
        max_articles = max(int(source.get('max_articles', 20)), 0)
        for entry in entries[:max_articles]:
            news = self._parse_rss_entry(entry, source)
            if not news:
                skipped_invalid += 1
                continue

            source_exclude = source.get('exclude_keywords', [])
            if source_exclude:
                text = f"{news['title']} {news['summary']}".lower()
                if any(str(keyword).lower() in text for keyword in source_exclude):
                    skipped_keyword += 1
                    continue

            should_fetch_content = source.get('fetch_full_content', True)
            if (
                should_fetch_content
                and self._is_external_link(news['link'])
                and (not news['summary'] or len(news['summary']) < 100)
            ):
                page_title, full_content = self._fetch_full_content(news['link'], source)
                if full_content:
                    news['summary'] = full_content
                    if page_title and len(page_title) >= 8:
                        news['title'] = page_title

            news_list.append(news)

        logger.info(
            f"RSS解析 {source['name']}: 条目={len(entries)}, 读取={min(len(entries), max_articles)}, "
            f"接受={len(news_list)}, 字段无效={skipped_invalid}, 关键词排除={skipped_keyword}"
        )
        return news_list

    def _collect_by_scraping(self, source: Dict[str, Any]) -> List[Dict[str, Any]]:
        news_list = []
        try:
            url = source['url']
            response = self._request(url, source, timeout=15)
            if response.status_code != 200:
                logger.warning(f"抓取 {source['name']} 首页失败: HTTP {response.status_code}")
                return news_list

            soup = BeautifulSoup(response.content, 'html.parser')

            article_link_patterns = source.get('link_patterns', ['/20', 'article', 'detail', 'content'])
            max_articles = source.get('max_articles', 20)

            seen_urls = set()
            for a in soup.find_all('a', href=True):
                href = a['href']
                title = a.get_text(strip=True)

                if not title or len(title) < 8 or len(title) > 100:
                    continue

                if not any(pattern in href for pattern in article_link_patterns):
                    continue

                full_url = urljoin(url, href)
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)

                if len(news_list) >= max_articles:
                    break

                page_title, summary = self._fetch_full_content(full_url, source)
                if not summary or len(summary) < 50:
                    continue

                title = page_title if page_title and len(page_title) >= 8 else title

                content_hash = self._generate_hash(title + full_url)
                now = datetime.now(timezone(timedelta(hours=8)))
                news = {
                    'title': title,
                    'summary': summary,
                    'link': full_url,
                    'source': source['name'],
                    'published': now.isoformat(),
                    'collected_at': now.isoformat(),
                    'category': source.get('category', '未分类'),
                    'content_hash': content_hash
                }
                news_list.append(news)

        except Exception as e:
            logger.error(f"网页抓取 {source['name']} 时出错: {str(e)}")

        return news_list

    def _parse_rss_entry(self, entry: Dict[str, Any], source: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            title = str(entry.get('title', '') or '').strip()
            link = str(entry.get('link', '') or '').strip()
            guid = str(entry.get('id', '') or entry.get('guid', '') or '').strip()
            published = str(
                entry.get('published', '') or entry.get('pubDate', '') or entry.get('updated', '') or ''
            ).strip()
            summary = str(entry.get('summary', '') or entry.get('description', '') or '').strip()

            tz_cn = timezone(timedelta(hours=8))
            published_date = None
            parsed_time = entry.get('published_parsed') or entry.get('updated_parsed') or entry.get('pubdate_parsed')
            if parsed_time:
                published_date = datetime(*parsed_time[:6], tzinfo=timezone.utc)
            elif published:
                try:
                    import dateutil.parser
                    published_date = dateutil.parser.parse(published)
                    if published_date.tzinfo is None:
                        published_date = published_date.replace(tzinfo=timezone.utc)
                except Exception as exc:
                    logger.warning(f"{source['name']} 发布时间解析失败，使用采集时间: {published} ({exc})")
            if published_date is None:
                published_date = datetime.now(tz_cn)

            content = ''
            entry_content = entry.get('content') or []
            if entry_content:
                content = ' '.join(str(item.get('value', '') or '') for item in entry_content)
            elif entry.get('content:encoded'):
                content = str(entry.get('content:encoded') or '')

            summary = self._clean_html(summary)
            content = self._clean_html(content)
            if not summary and content:
                summary = content

            if not title:
                return None

            published_iso = published_date.isoformat()
            if not link:
                if not source.get('allow_missing_link', False):
                    return None
                stable_text = '\n'.join([
                    guid or title,
                    published_iso,
                    ' '.join(summary.split()),
                ])
                digest = hashlib.sha256(stable_text.encode('utf-8')).hexdigest()
                prefix = str(source.get('synthetic_link_prefix', 'urn:newsflow:item')).rstrip(':')
                link = f"{prefix}:{digest}"
                content_hash = digest
            else:
                content_hash = self._generate_hash(title + link)

            collected_at = datetime.now(tz_cn).isoformat()
            return {
                'title': title,
                'summary': summary,
                'link': link,
                'source': source['name'],
                'published': published_iso,
                'collected_at': collected_at,
                'category': source.get('category', '未分类'),
                'content_hash': content_hash,
            }

        except Exception as exc:
            logger.error(f"解析 {source.get('name', '未知来源')} 新闻条目时出错: {exc}")
            return None

    def _fetch_full_content(self, url: str, source: Optional[Dict[str, Any]] = None) -> tuple:
        try:
            if not self._is_external_link(url):
                return ('', '')
            response = self._request(url, source or {}, timeout=15)
            if response.status_code != 200:
                return ('', '')

            soup = BeautifulSoup(response.content, 'html.parser')

            page_title = ''
            title_tag = soup.find('title')
            if title_tag and title_tag.get_text(strip=True):
                page_title = title_tag.get_text(strip=True)
            else:
                h1 = soup.find('h1')
                if h1 and h1.get_text(strip=True):
                    page_title = h1.get_text(strip=True)

            for tag in soup(['script', 'style', 'nav', 'header', 'footer', 'aside',
                             'iframe', 'noscript', 'form', 'button', 'input',
                             'div[class*="comment"]', 'div[class*="share"]',
                             'div[class*="recommend"]', 'div[class*="related"]',
                             'div[class*="sidebar"]', 'div[class*="ad"]',
                             'div[class*="banner"]', 'div[class*="popup"]',
                             'div[class*="modal"]', 'div[class*="tag"]',
                             'span[class*="tag"]', 'div[class*="breadcrumb"]',
                             'div[class*="copyright"]', 'div[class*="source"]',
                             'div[class*="author"]', 'div[class*="editor"]',
                             'div[class*="keyword"]', 'div[class*="hot"]',
                             'div[class*="more"]', 'div[class*="list"]']):
                tag.decompose()

            selectors = [
                'article',
                'div[class*="content"]',
                'div[class*="article"]',
                'div[class*="detail"]',
                'div[class*="text"]',
                'div[class*="body"]',
                'div[id*="content"]',
                'div[id*="article"]',
                'main',
            ]

            for selector in selectors:
                content_element = soup.select_one(selector)
                if content_element:
                    paragraphs = content_element.find_all(['p'])
                    if paragraphs:
                        texts = []
                        for p in paragraphs[:6]:
                            t = p.get_text(strip=True)
                            if len(t) > 20 and not self._is_noise(t):
                                texts.append(t)
                        if texts:
                            return (page_title, ' '.join(texts)[:800])
                    text = content_element.get_text(separator=' ', strip=True)
                    if len(text) > 100:
                        return (page_title, self._clean_extracted_text(text)[:800])

            paragraphs = soup.find_all(['p'])
            if paragraphs:
                texts = []
                for p in paragraphs[:6]:
                    t = p.get_text(strip=True)
                    if len(t) > 20 and not self._is_noise(t):
                        texts.append(t)
                if texts:
                    return (page_title, ' '.join(texts)[:800])

            return (page_title, '')
        except Exception as e:
            logger.error(f"获取网页内容时出错: {str(e)}")
            return ('', '')

    def _is_noise(self, text: str) -> bool:
        noise_patterns = [
            '相关报道', '相关推荐', '相关新闻', '相关文章',
            '点击查看', '点击下载', '扫码下载', '扫码关注',
            '责任编辑', '编辑：', '记者：', '来源：',
            '延伸阅读', '热门推荐', '猜你喜欢',
            '【财新周刊】', '【财新网】',
        ]
        text_lower = text.strip()
        for pattern in noise_patterns:
            if text_lower.startswith(pattern):
                return True
        if text_lower.startswith('【') and '】' in text_lower[:20]:
            return True
        return False

    def _clean_extracted_text(self, text: str) -> str:
        import re
        text = re.sub(r'相关报道.*', '', text)
        text = re.sub(r'相关推荐.*', '', text)
        text = re.sub(r'相关新闻.*', '', text)
        text = re.sub(r'延伸阅读.*', '', text)
        text = re.sub(r'热门推荐.*', '', text)
        text = re.sub(r'责任编辑.*', '', text)
        text = re.sub(r'【[^】]*】', '', text)
        text = re.sub(r'专享[^，。]*[，。]', '', text)
        text = re.sub(r'解锁直达.*', '', text)
        text = re.sub(r'单篇付费.*', '', text)
        text = re.sub(r'精粹专家视角.*', '', text)
        text = re.sub(r'点击[^，。]*[，。]', '', text)
        text = re.sub(r'扫描[^，。]*[，。]', '', text)
        text = re.sub(r'下载[^，。]*[，。]', '', text)
        text = re.sub(r'关注[^，。]*[，。]', '', text)
        text = re.sub(r'分享到.*', '', text)
        text = re.sub(r'版权所有.*', '', text)
        text = re.sub(r'未经授权.*', '', text)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()

    def _clean_html(self, html: str) -> str:
        try:
            soup = BeautifulSoup(html, 'html.parser')
            for tag in soup(['script', 'style', 'a', 'img', 'div[class*="related"]',
                             'div[class*="recommend"]', 'div[class*="share"]']):
                tag.decompose()
            text = soup.get_text(separator=' ', strip=True)
            text = self._clean_extracted_text(text)
            return text
        except Exception:
            return html

    def _generate_hash(self, text: str) -> str:
        return hashlib.md5(text.encode('utf-8')).hexdigest()

    def _deduplicate_news(self, news_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen_hashes = set()
        unique_news = []

        for news in news_list:
            if news['content_hash'] not in seen_hashes:
                seen_hashes.add(news['content_hash'])
                unique_news.append(news)

        return unique_news


if __name__ == "__main__":
    collector = NewsCollector()
    news = collector.collect_news()
    print(f"采集到 {len(news)} 条新闻")
    for i, item in enumerate(news[:5]):
        print(f"\n{i+1}. {item['title']}")
        print(f"   来源: {item['source']}")
        print(f"   链接: {item['link']}")
        print(f"   发布时间: {item['published']}")
        print(f"   摘要: {item['summary'][:100]}...")
