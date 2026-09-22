import sqlite3
import os
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from ...core.config.config import settings
from dateutil import parser as dateutil_parser
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class NewsStorage:

    def __init__(self):
        self.db_path = settings.database_path
        self._ensure_data_directory()
        self._init_database()

    def _ensure_data_directory(self):
        data_dir = os.path.dirname(self.db_path)
        if data_dir and not os.path.exists(data_dir):
            os.makedirs(data_dir, exist_ok=True)
            logger.info(f"创建数据目录: {data_dir}")

    def _init_database(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS news (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title TEXT NOT NULL,
                        summary TEXT,
                        link TEXT UNIQUE NOT NULL,
                        source TEXT NOT NULL,
                        published DATETIME,
                        collected_at DATETIME NOT NULL,
                        category TEXT,
                        content_hash TEXT UNIQUE NOT NULL,
                        title_original TEXT,
                        summary_original TEXT,
                        translated INTEGER DEFAULT 0
                    )
                ''')

                try:
                    cursor.execute('ALTER TABLE news ADD COLUMN title_original TEXT')
                except sqlite3.OperationalError:
                    pass
                try:
                    cursor.execute('ALTER TABLE news ADD COLUMN summary_original TEXT')
                except sqlite3.OperationalError:
                    pass
                try:
                    cursor.execute('ALTER TABLE news ADD COLUMN translated INTEGER DEFAULT 0')
                except sqlite3.OperationalError:
                    pass

                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS news_sources (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT UNIQUE NOT NULL,
                        url TEXT UNIQUE NOT NULL,
                        enabled BOOLEAN DEFAULT 1,
                        category TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS filter_rules (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        type TEXT NOT NULL,
                        value TEXT NOT NULL,
                        priority INTEGER DEFAULT 0,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS newsletters (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        date DATE NOT NULL,
                        title TEXT NOT NULL,
                        content TEXT,
                        generated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        format TEXT NOT NULL
                    )
                ''')

                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS event_fingerprints (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_key TEXT NOT NULL,
                        first_seen DATETIME NOT NULL,
                        last_seen DATETIME NOT NULL,
                        kept_source TEXT NOT NULL,
                        kept_title TEXT NOT NULL,
                        kept_link TEXT,
                        event_date DATE NOT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                cursor.execute('CREATE INDEX IF NOT EXISTS idx_news_content_hash ON news(content_hash)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_news_published ON news(published)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_news_source ON news(source)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_news_category ON news(category)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_newsletters_date ON newsletters(date)')

                conn.commit()
                logger.info("数据库初始化完成")

        except Exception as e:
            logger.error(f"初始化数据库时出错: {str(e)}")

    def save_news(self, news_list: List[Dict[str, Any]]) -> tuple[int, int]:
        saved_count = 0
        skipped_count = 0

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                for news in news_list:
                    try:
                        cursor.execute('SELECT id FROM news WHERE content_hash = ?', (news['content_hash'],))
                        if cursor.fetchone():
                            skipped_count += 1
                            continue

                        cursor.execute('''
                            INSERT INTO news (title, summary, link, source, published, collected_at, category, content_hash, title_original, summary_original, translated)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            news['title'],
                            news['summary'],
                            news['link'],
                            news['source'],
                            news['published'],
                            news['collected_at'],
                            news['category'],
                            news['content_hash'],
                            news.get('title_original', ''),
                            news.get('summary_original', ''),
                            1 if news.get('title_original') else 0
                        ))

                        saved_count += 1

                    except sqlite3.IntegrityError:
                        skipped_count += 1
                        continue
                    except Exception as e:
                        logger.error(f"保存新闻时出错: {str(e)}")

                conn.commit()
                logger.info(f"成功保存 {saved_count} 条新闻，跳过 {skipped_count} 条重复")

        except Exception as e:
            logger.error(f"保存新闻列表时出错: {str(e)}")

        return (saved_count, skipped_count)

    def update_translation(self, news_id: int, title: str, summary: str, title_original: str, summary_original: str) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE news SET title = ?, summary = ?, title_original = ?, summary_original = ?, translated = 1
                    WHERE id = ?
                ''', (title, summary, title_original, summary_original, news_id))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"更新翻译时出错: {str(e)}")
            return False

    def get_news(self, limit: int = 100, offset: int = 0, category: Optional[str] = None, source: Optional[str] = None) -> List[Dict[str, Any]]:
        news_list = []

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                if category:
                    query = 'SELECT * FROM news WHERE category = ?'
                    params: list = [category]
                    if source:
                        query += ' AND source = ?'
                        params.append(source)
                    query += ' ORDER BY published DESC NULLS LAST, collected_at DESC LIMIT ? OFFSET ?'
                    params.extend([limit, offset])
                    cursor.execute(query, params)
                    rows = cursor.fetchall()
                    news_list = [self._row_to_dict(row) for row in rows]
                elif source:
                    query = 'SELECT * FROM news WHERE source = ? ORDER BY published DESC NULLS LAST, collected_at DESC LIMIT ? OFFSET ?'
                    cursor.execute(query, (source, limit, offset))
                    rows = cursor.fetchall()
                    news_list = [self._row_to_dict(row) for row in rows]
                else:
                    cursor.execute('SELECT DISTINCT category FROM news')
                    all_cats = [row[0] for row in cursor.fetchall()]
                    ordered_cats = [c for c in settings.category_order if c in all_cats]
                    for c in all_cats:
                        if c not in ordered_cats:
                            ordered_cats.append(c)

                    # 国内分类（中文/财经）获得更高配额，确保国内源新闻不被外媒淹没
                    domestic_cats = {'中文', '财经'}
                    n_domestic = sum(1 for c in ordered_cats if c in domestic_cats)
                    n_other = len(ordered_cats) - n_domestic

                    # 财经分类 25 条，中文（新华社）仅 15 条（新华社软性内容多，过多反而降低筛选效率）
                    per_cat_quota = {'财经': 25, '中文': 15}
                    default_domestic_quota = 20

                    ordered = []
                    domestic_total_quota = sum(per_cat_quota.get(c, default_domestic_quota) for c in ordered_cats if c in domestic_cats)
                    for cat in ordered_cats:
                        if cat in per_cat_quota:
                            quota = per_cat_quota[cat]
                        elif cat in domestic_cats:
                            quota = default_domestic_quota
                        else:
                            quota = max((limit - domestic_total_quota) // max(n_other, 1), 5)

                        cursor.execute('''
                            SELECT * FROM news
                            WHERE category = ?
                            ORDER BY published DESC NULLS LAST, collected_at DESC
                            LIMIT ?
                        ''', (cat, quota))
                        rows = cursor.fetchall()
                        ordered.extend([self._row_to_dict(row) for row in rows])

                    ordered.sort(key=lambda x: x.get('published', '') or x.get('collected_at', ''), reverse=True)
                    news_list = ordered[:limit]

        except Exception as e:
            logger.error(f"获取新闻时出错: {str(e)}")

        return news_list

    def get_news_stats(self) -> Dict[str, Any]:
        stats = {'total': 0, 'by_source': {}, 'by_category': {}, 'translated': 0}
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT COUNT(*) FROM news')
                stats['total'] = cursor.fetchone()[0]
                cursor.execute('SELECT source, COUNT(*) FROM news GROUP BY source')
                stats['by_source'] = dict(cursor.fetchall())
                cursor.execute('SELECT category, COUNT(*) FROM news GROUP BY category')
                stats['by_category'] = dict(cursor.fetchall())
                cursor.execute('SELECT COUNT(*) FROM news WHERE translated = 1')
                stats['translated'] = cursor.fetchone()[0]
        except Exception as e:
            logger.error(f"获取统计信息时出错: {str(e)}")
        return stats

    def _row_to_dict(self, row) -> Dict[str, Any]:
        tz_cn = timezone(timedelta(hours=8))
        tz_utc = timezone.utc
        published = row['published']
        collected_at = row['collected_at']
        category = row['category'] if 'category' in row.keys() else ''
        is_foreign = category == '英文'

        if published:
            try:
                dt = dateutil_parser.parse(str(published))
                if dt.tzinfo is None:
                    if is_foreign:
                        dt = dt.replace(tzinfo=tz_utc)
                    else:
                        dt = dt.replace(tzinfo=tz_cn)
                published = dt.astimezone(tz_cn).strftime('%Y-%m-%d %H:%M')
            except Exception:
                pass
        if collected_at:
            try:
                dt = dateutil_parser.parse(str(collected_at))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=tz_cn)
                collected_at = dt.astimezone(tz_cn).strftime('%Y-%m-%d %H:%M')
            except Exception:
                pass

        return {
            'id': row['id'],
            'title': row['title'],
            'summary': row['summary'],
            'link': row['link'],
            'source': row['source'],
            'published': published,
            'collected_at': collected_at,
            '_published_timezone': 'Asia/Shanghai' if published else '',
            'category': row['category'],
            'content_hash': row['content_hash'],
            'title_original': row['title_original'] if 'title_original' in row.keys() else '',
            'summary_original': row['summary_original'] if 'summary_original' in row.keys() else '',
            'translated': row['translated'] if 'translated' in row.keys() else 0,
        }

    def clean_old_news(self, days: int = 7) -> int:
        deleted_count = 0

        try:
            tz_cn = timezone(timedelta(hours=8))
            now = datetime.now(tz_cn)
            cutoff_date = (now - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%S')

            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                cursor.execute('DELETE FROM news WHERE collected_at < ?', (cutoff_date,))
                deleted_count = cursor.rowcount

                conn.commit()
                logger.info(f"清理了 {deleted_count} 条旧新闻")

        except Exception as e:
            logger.error(f"清理旧新闻时出错: {str(e)}")

        return deleted_count

    def save_newsletter(self, newsletter: Dict[str, Any]) -> Optional[int]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                cursor.execute('DELETE FROM newsletters WHERE date = ?', (newsletter['date'],))

                cursor.execute('''
                    INSERT INTO newsletters (date, title, content, format, generated_at)
                    VALUES (?, ?, ?, ?, ?)
                ''', (
                    newsletter['date'],
                    newsletter['title'],
                    newsletter['content'],
                    newsletter['format'],
                    newsletter.get('generated_at', datetime.now(timezone(timedelta(hours=8))).isoformat())
                ))

                conn.commit()
                logger.info(f"成功保存简报: {newsletter['title']}")
                return cursor.lastrowid

        except Exception as e:
            logger.error(f"保存简报时出错: {str(e)}")
            return None

    def get_newsletter(self, date: str) -> Optional[Dict[str, Any]]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                cursor.execute('SELECT * FROM newsletters WHERE date = ?', (date,))
                row = cursor.fetchone()

                if row:
                    generated_at = row['generated_at']
                    if generated_at:
                        try:
                            dt = dateutil_parser.parse(str(generated_at))
                            tz_cn = timezone(timedelta(hours=8))
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=tz_cn)
                            generated_at = dt.astimezone(tz_cn).strftime('%Y-%m-%d %H:%M:%S')
                        except Exception:
                            pass

                    return {
                        'id': row['id'],
                        'date': row['date'],
                        'title': row['title'],
                        'content': row['content'],
                        'generated_at': generated_at,
                        'format': row['format']
                    }

        except Exception as e:
            logger.error(f"获取简报时出错: {str(e)}")

        return None

    def get_news_by_date_range(self, start_date: str, end_date: str, category: Optional[str] = None, source: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        news_list = []
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                query = 'SELECT * FROM news WHERE date(collected_at) >= ? AND date(collected_at) <= ?'
                params = [start_date, end_date]

                if category:
                    query += ' AND category = ?'
                    params.append(category)
                if source:
                    query += ' AND source = ?'
                    params.append(source)

                query += ' ORDER BY category, published DESC NULLS LAST, collected_at DESC LIMIT ?'
                params.append(limit)

                cursor.execute(query, params)
                rows = cursor.fetchall()
                news_list = [self._row_to_dict(row) for row in rows]

        except Exception as e:
            logger.error(f"按日期范围查询新闻时出错: {str(e)}")

        return news_list

    def get_daily_stats(self, days: int = 30) -> List[Dict[str, Any]]:
        stats_list = []
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT date(collected_at) as date, COUNT(*) as count,
                           GROUP_CONCAT(DISTINCT source) as sources
                    FROM news
                    WHERE collected_at >= date('now', '-' || ? || ' days')
                    GROUP BY date(collected_at)
                    ORDER BY date DESC
                ''', (days,))
                for row in cursor.fetchall():
                    stats_list.append({
                        'date': row['date'],
                        'count': row['count'],
                        'sources': row['sources']
                    })
        except Exception as e:
            logger.error(f"获取每日统计时出错: {str(e)}")
        return stats_list

    def save_event_fingerprint(self, event_key: str, source: str, title: str, link: str, event_date: str) -> bool:
        tz_cn = timezone(timedelta(hours=8))
        now = datetime.now(tz_cn).isoformat()
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT id FROM event_fingerprints WHERE event_key = ?', (event_key,))
                existing = cursor.fetchone()
                if existing:
                    cursor.execute('''
                        UPDATE event_fingerprints
                        SET last_seen = ?, kept_source = ?, kept_title = ?, kept_link = ?
                        WHERE event_key = ?
                    ''', (now, source, title, link, event_key))
                else:
                    cursor.execute('''
                        INSERT INTO event_fingerprints (event_key, first_seen, last_seen, kept_source, kept_title, kept_link, event_date)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (event_key, now, now, source, title, link, event_date))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"保存事件指纹时出错: {str(e)}")
            return False

    def remember_published_events(self, news_list: List[Dict[str, Any]]) -> int:
        """Persist stable event keys only for items that were actually published."""
        saved = 0
        for news in news_list:
            event_key = str(news.get('event_key', '') or '').strip()
            if not event_key:
                continue
            event_date = str(news.get('published', '') or '')[:10]
            if not event_date:
                event_date = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d')
            if self.save_event_fingerprint(
                event_key,
                str(news.get('source', '') or ''),
                str(news.get('title', '') or ''),
                str(news.get('link', '') or ''),
                event_date,
            ):
                saved += 1
        self.clean_old_events(days=14)
        logger.info(f"已记录 {saved} 条实际发布事件")
        return saved

    def get_recent_events(self, days: int = 7) -> Dict[str, Dict[str, Any]]:
        tz_cn = timezone(timedelta(hours=8))
        cutoff = (datetime.now(tz_cn) - timedelta(days=days)).isoformat()
        events = {}
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT event_key, kept_source, kept_title, kept_link, event_date, last_seen
                    FROM event_fingerprints
                    WHERE last_seen >= ?
                    ORDER BY last_seen DESC
                ''', (cutoff,))
                for row in cursor.fetchall():
                    events[row['event_key']] = {
                        'source': row['kept_source'],
                        'title': row['kept_title'],
                        'link': row['kept_link'],
                        'event_date': row['event_date'],
                        'last_seen': row['last_seen']
                    }
        except Exception as e:
            logger.error(f"获取历史事件时出错: {str(e)}")
        return events

    def clean_old_events(self, days: int = 14) -> int:
        tz_cn = timezone(timedelta(hours=8))
        cutoff = (datetime.now(tz_cn) - timedelta(days=days)).isoformat()
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM event_fingerprints WHERE last_seen < ?', (cutoff,))
                deleted = cursor.rowcount
                conn.commit()
                if deleted > 0:
                    logger.info(f"清理了 {deleted} 条过期事件指纹")
                return deleted
        except Exception as e:
            logger.error(f"清理过期事件时出错: {str(e)}")
            return 0
