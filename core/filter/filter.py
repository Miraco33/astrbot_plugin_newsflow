import json
import os
import re
import time
import asyncio
from difflib import SequenceMatcher
from typing import List, Dict, Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from ...core.config.config import settings
from ...core.storage.storage import NewsStorage
import logging

# 统一排除词表 — _coarse_filter 和 _keyword_filter 共用
DEFAULT_EXCLUDE_KEYWORDS = [
    # 娱乐/体育/八卦
    '娱乐', '体育', '八卦', '明星', '综艺', '选秀', '球赛', '足球', '篮球',
    # 付费墙标记
    '专享', '解锁', '研选', 'VIP', 'vip', '付费', '订阅', '会员专享',
    # 聚合/复盘/观点类
    'T早报', '早报', '早知道', '快讯集锦', '收盘', '复盘', '观点文章', '深度分析',
    # 展会/旅游/乡村/农事
    '消博', '博览会', '旅游', '茶山', '振兴', '农事', '乡村', '县域经济',
    # 城市宣传/风光/摄影
    '城市', '之城', '风光', '摄影', '无人机照片',
    # 季节性/节日
    '春日', '春暖', '花开', '踏青', '赏花', '五一', '假期',
    # 校园/教育
    '学校', '大学', '校友', '学长', '教育改革',
    # 新华社类软性内容
    '潮玩', '舞蹈', '夺冠', '奖牌', '金牌', '银牌', '致敬', '献礼',
    # 新媒体/低质内容标记
    'Vlog', 'vlog', '首发', '打卡', '美好生活', '城市介绍', '人物特写',
    '学校宣传', '革命圣地', '历史故事', '文化报道',
    # 特殊排除（原_keyword_filter独有）
    '茶叶', '助手还是', '竞争对手', '得力助手', '如何被', '是得力',
    '观察', '思考', '解读', '分析', '评论',
]

logger = logging.getLogger(__name__)

KEY_FIGURE_RE = re.compile(
    r'(?<![A-Za-z0-9.])(?:约|近|超|逾|至少|最多|超过|不足)?'
    r'(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*'
    r'(?:%|％|个百分点|个基点|基点|万亿|亿元|万元|亿港元|万港元|'
    r'亿美元|万美元|欧元|英镑|港元|人民币|美元|元|万人|人|家|项|座|'
    r'艘|架|辆|台|吨|桶|天|年|个月|月|周|倍|bps?|million|billion|'
    r'trillion|percent)',
    re.IGNORECASE,
)

THIRD_PARTY_OPINION_RE = re.compile(
    r'券商|证券公司|分析师|策略师|研究员|首席经济学家|投行|机构观点|'
    r'brokerage|broker|analyst|strategist|researcher|investment bank|economist',
    re.IGNORECASE,
)
OPINION_LANGUAGE_RE = re.compile(
    r'认为|预计|预期|预测|看好|警告|研判|观点|称.{0,12}(?:可能|或将|将会)|'
    r'believes?|expects?|forecasts?|predicts?|sees?\b|warns?|outlook|view',
    re.IGNORECASE,
)
VAGUE_RESEARCH_CLAIM_RE = re.compile(
    r'研究显示|研究称|报告认为|报告称|调查认为|机构称|'
    r'(?:study|research)\s+(?:shows?|finds?|suggests?)|report\s+(?:says?|suggests?)',
    re.IGNORECASE,
)
FACTUAL_ACTION_RE = re.compile(
    r'公布|发布|宣布|批准|签署|通过|实施|生效|达成|收购|出售|裁员|'
    r'起诉|裁决|驳回|下调|上调|增持|减持|回购|融资|投产|交付|召回|'
    r'数据显示|报告显示|reported|announced|approved|signed|passed|enacted|'
    r'acquired|sold|cut jobs|filed|ruled|rejected|raised|lowered|launched|'
    r'delivered|recalled|data show',
    re.IGNORECASE,
)


def _key_figure_numbers(value: str) -> set[str]:
    numbers = set()
    for match in KEY_FIGURE_RE.finditer(str(value or '')):
        number_match = re.search(r'\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?', match.group())
        if number_match:
            numbers.add(number_match.group().replace(',', ''))
    return numbers


def _strip_speculative_impact_tail(value: str) -> str:
    text = str(value or '').strip()
    tail = re.compile(
        r'[，,；;]\s*(?:这|此|该(?:事件|决定|政策|行动|交易|裁决|数据|结果|举措))?\s*'
        r'(?:可能|可|或将|或会|或可|预计将|预计会|预计可能|可望|有望)\s*'
        r'[^，。；;]{0,80}(?:影响|冲击|压力|提振|利好|利空|改变|扰动|'
        r'推高|压低|加剧|缓解|支撑|拖累|刺激)[^，。；;]*[。.]?$',
    )
    english_tail = re.compile(
        r'[,;]\s*(?:which|this|the move|the decision)?\s*'
        r'(?:may|might|could|is expected to)\s+[^.;]{0,100}'
        r'(?:affect|impact|boost|weigh on|pressure|support|disrupt)[^.;]*[.]?$',
        re.IGNORECASE,
    )
    previous = None
    while text and text != previous:
        previous = text
        text = tail.sub('', text).strip()
        text = english_tail.sub('', text).strip()
    return text.rstrip('，,；;')


def _summary_with_key_figures(news: Dict[str, Any], value: str) -> str:
    summary = _strip_speculative_impact_tail(value)
    source_text = f"{news.get('title', '')}。{news.get('summary', '') or ''}"
    source_numbers = _key_figure_numbers(source_text)
    if not source_numbers or source_numbers & _key_figure_numbers(summary):
        return summary

    clauses = [
        clause.strip()
        for clause in re.split(r'[。！？!?；;\n]+', source_text)
        if _key_figure_numbers(clause)
    ]
    if not clauses:
        return summary
    clause = max(clauses, key=lambda item: (len(_key_figure_numbers(item)), -len(item)))
    if len(clause) > 100:
        first_figure = KEY_FIGURE_RE.search(clause)
        center = first_figure.start() if first_figure else 0
        start = max(0, center - 35)
        clause = clause[start:start + 100].strip(' ，,；;')
    clause = _strip_speculative_impact_tail(clause)
    if not clause:
        return summary
    if not summary:
        return clause
    if clause.lower() in summary.lower():
        return summary
    separator = '; ' if re.search(r'[A-Za-z]', clause) else '；'
    return f"{summary.rstrip('。.')}{separator}{clause}"

FILTER_SYSTEM_PROMPT = """你是面向金融市场、宏观研究和产业研究读者的新闻晨报主编。你的目标不是挑选最轰动的新闻，而是在有限版面内形成均衡、可决策的新闻组合。

【核心范围】
1. 宏观数据与政策监管：经济数据、货币财政政策、贸易与金融监管
2. 财经市场：利率、汇率、股票、债券、商品、银行和资本市场
3. 产业与公司：产能、供应链、并购、业绩、融资、重大管理层和商业模式变化
4. 科技创新：AI、半导体、软件、通信、生物科技、能源技术、汽车和航天
5. 地缘政治仅作辅助：只有重大升级，或能通过能源、航运、制裁、供应链、通胀、汇率、利率等渠道影响市场时才高分

【必须降级或排除】
1. 礼节性访问、视察调研、宣传通稿、人物特写、纪念活动
2. 纯表态、纯观点或没有新数据和新行动的分析文章；普通券商、分析师、策略师和研究员的预测或评论，没有可核验数据或新增事实时必须排除。央行行长、美联储主席等有政策决定权者的正式政策信号不按普通分析师处理
3. 同一长期冲突的重复战况、口头威胁、采访角度和实时滚动更新；必须说明相对前序报道新增了什么
4. 普通战况更新若没有重大升级或明确市场传导，最高不超过5分
5. 体育、娱乐、社会软闻以及与市场无明显关系的事故和环境议题
6. 国内政治人事、确认听证、忠诚度争议和一般司法案件，若未改变监管、财政、贸易或产业政策，应排除
7. AI滥用个案只有形成平台责任、监管先例或重大公司风险时才进入核心版面

【评分维度】
- 市场/经济/产业实际影响：30%
- 相对近期报道的信息增量：25%
- 对决策者的相关性：20%
- 数据与行动的具体程度：10%
- 来源可靠性和原创程度：10%
- 时效性：5%

【分类枚举】
只允许：政策监管、财经市场、科技产业、国际局势、其他。
其中产业与公司新闻归入“科技产业”；石油、天然气、航运和大宗商品的合作、供给与价格变化优先归入“财经市场”或“科技产业”，只有战争或外交升级本身才归入“国际局势”。

【标识要求】
- story_key：同一长期故事必须使用稳定、宽口径的短标识，例如同一战争、同一贸易争端、同一央行政策周期
- event_key：同一具体事件必须使用稳定、窄口径的短标识
- 不要因来源是国际大媒体而自动提高分数
- summary对中文源和外文源都必须重新概括为一句中文，目标40-60字、最多80字（含标点）；输出前检查字数，超长时删减次要背景并重新组织为完整句子，不使用省略号；只写核心主体、行动、结果和最重要的决策相关数字，不照抄正文或罗列背景。不要在末尾补写“可能影响/或将提振/可能造成冲击”等推断，影响判断只体现在impact_score和reason中

请严格返回JSON，不要输出额外文字。"""


def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    json_match = re.search(r'\{[\s\S]*\}', text)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass
    return None


class AIClient:
    _clients: Dict[str, Any] = {}

    def __init__(self):
        self.provider = settings.ai_provider
        self.api_key = settings.ai_api_key
        self.api_base = settings.ai_api_base
        self.model = settings.ai_model

        if self.provider == 'deepseek':
            if not self.api_base or self.api_base == 'https://api.openai.com/v1':
                self.api_base = 'https://api.deepseek.com'
            if not self.model or self.model == 'gpt-4o-mini':
                self.model = 'deepseek-v4-flash'
        elif self.provider == 'gemini':
            if not self.api_base or self.api_base == 'https://api.openai.com/v1':
                self.api_base = 'https://generativelanguage.googleapis.com/v1beta/'
            if not self.model or self.model == 'gpt-4o-mini':
                self.model = 'gemini-3.1-flash-lite'
        elif self.provider == 'zhipu':
            if not self.api_base or self.api_base in ('https://api.openai.com/v1', 'https://open.bigmodel.cn/api/m/v1'):
                self.api_base = 'https://open.bigmodel.cn/api/paas/v4/'
            if not self.model or self.model == 'gpt-4o-mini':
                self.model = 'glm-4-flash'
        elif self.provider == 'groq':
            if not self.api_base or self.api_base == 'https://api.openai.com/v1':
                self.api_base = 'https://api.groq.com/openai/v1'
            if not self.model or self.model == 'gpt-4o-mini':
                self.model = 'llama-3.3-70b-versatile'
        elif self.provider == 'qwen':
            if not self.api_base or self.api_base == 'https://api.openai.com/v1':
                self.api_base = 'https://api.siliconflow.cn/v1'
            if not self.model or self.model == 'gpt-4o-mini':
                self.model = 'Qwen/Qwen3-8B'

        if self.provider == 'qwen':
            self.request_delay = 1
            self.max_retries = 3
            self.batch_size = 10
        elif self.provider == 'gemini':
            self.request_delay = 5
            self.max_retries = 5
            self.batch_size = 5
        elif self.provider == 'zhipu':
            self.request_delay = 3
            self.max_retries = 3
            self.batch_size = 10
        elif self.provider == 'groq':
            self.request_delay = 12
            self.max_retries = 3
            self.batch_size = 5
        elif self.provider == 'deepseek':
            self.request_delay = 0.5
            self.max_retries = 3
            self.batch_size = 10
        else:
            self.request_delay = 2
            self.max_retries = 3
            self.batch_size = 15

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def chat(self, system_prompt: str, user_prompt: str, temperature: float = 0.1, max_tokens: int = 2000, json_mode: bool = False) -> Optional[str]:
        if not self.is_configured():
            return None
        return self._openai_compatible(system_prompt, user_prompt, temperature, max_tokens, json_mode)

    async def chat_async(self, system_prompt: str, user_prompt: str, temperature: float = 0.1, max_tokens: int = 2000, json_mode: bool = False) -> Optional[str]:
        if not self.is_configured():
            return None
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self._openai_compatible,
            system_prompt, user_prompt, temperature, max_tokens, json_mode
        )

    def _get_client(self) -> Any:
        import openai
        import httpx

        key = (self.provider, self.api_base, self.api_key)
        if key not in AIClient._clients:
            if self.provider == 'groq':
                http_client = httpx.Client(
                    timeout=httpx.Timeout(60.0),
                    limits=httpx.Limits(max_connections=1, max_keepalive_connections=1)
                )
            elif self.provider == 'deepseek':
                http_client = httpx.Client(timeout=30.0)
            else:
                http_client = httpx.Client(timeout=60.0)
            AIClient._clients[key] = openai.OpenAI(
                api_key=self.api_key,
                base_url=self.api_base,
                http_client=http_client,
                max_retries=0
            )
        return AIClient._clients[key]

    def _openai_compatible(self, system_prompt: str, user_prompt: str, temperature: float, max_tokens: int, json_mode: bool) -> Optional[str]:
        try:
            client = self._get_client()

            kwargs = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            }

            if self.provider == 'qwen':
                kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
            elif self.provider == 'deepseek':
                kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

            if json_mode and self.provider not in ('groq', 'gemini'):
                kwargs["response_format"] = {"type": "json_object"}

            if json_mode and self.provider in ('groq', 'gemini'):
                kwargs["messages"][0]["content"] += "\n\n重要：你必须且只能返回合法的JSON格式，不要包含任何其他文字。"

            for attempt in range(self.max_retries):
                try:
                    start_time = time.time()
                    response = client.chat.completions.create(**kwargs)
                    elapsed = time.time() - start_time
                    choice = response.choices[0]
                    content = choice.message.content
                    finish_reason = getattr(choice, 'finish_reason', '') or ''
                    usage = getattr(response, 'usage', None)
                    completion_tokens = getattr(usage, 'completion_tokens', None) if usage else None
                    logger.info(
                        f"API 响应时间: {elapsed:.2f}秒, finish_reason={finish_reason or 'unknown'}, "
                        f"响应字符={len(content or '')}, completion_tokens={completion_tokens if completion_tokens is not None else 'unknown'}"
                    )
                    if finish_reason == 'length':
                        logger.warning("AI 响应达到输出长度上限，当前批次可能需要拆分重试")
                    if self.provider == 'qwen':
                        if not content:
                            content = getattr(response.choices[0].message, 'reasoning_content', None) or ''
                        content = re.sub(r'<think[\s\S]*?</think\s*>', '', content).strip()
                        if elapsed > 30:
                            logger.warning(f"Qwen 响应时间过长({elapsed:.1f}s)，可能触发了思考模式")
                    if self.provider == 'deepseek' and not content:
                        if json_mode and 'response_format' in kwargs:
                            logger.warning("DeepSeek json_mode 返回空响应，关闭 json_mode 重试")
                            kwargs.pop("response_format", None)
                            kwargs["messages"][0]["content"] += "\n\n你必须严格按JSON格式返回：{\"results\":{...}}"
                            time.sleep(0.5)
                            json_mode = False
                            continue
                        if attempt < self.max_retries - 1:
                            logger.warning(f"DeepSeek 返回空响应 (attempt {attempt+1})，1秒后重试")
                            time.sleep(1)
                            continue
                        logger.error("DeepSeek 多次返回空响应，批次丢弃")
                        return None
                    return content
                except Exception as e:
                    error_str = str(e)
                    is_payload_too_large = '413' in error_str or 'too large' in error_str.lower()
                    is_rate_limit = (
                        '429' in error_str
                        or 'rate' in error_str.lower()
                        or 'quota' in error_str.lower()
                        or 'resource_exhausted' in error_str.lower()
                        or 'too many' in error_str.lower()
                    )
                    is_retryable = (
                        'timed out' in error_str.lower()
                        or 'connection' in error_str.lower()
                        or 'timeout' in error_str.lower()
                    )
                    is_fatal = (
                        '403' in error_str
                        or '401' in error_str
                        or 'invalid' in error_str.lower()
                    )
                    logger.warning(f"API 调用异常 (attempt {attempt+1}): {error_str[:150]}")

                    if is_payload_too_large or is_fatal:
                        logger.error(f"不可重试错误({error_str[:50]})，直接失败")
                        return None

                    if is_rate_limit or is_retryable:
                        wait = self.request_delay * (2 ** attempt)
                        if wait > 60:
                            wait = 60
                        reason = "限流" if is_rate_limit else "超时/连接错误"
                        logger.warning(f"API {reason}，等待 {wait} 秒后重试 ({attempt+1}/{self.max_retries})")
                        time.sleep(wait)
                        continue
                    else:
                        logger.error(f"API 调用出错: {error_str[:200]}")
                        raise

            logger.error(f"API 调用失败，已重试 {self.max_retries} 次")
            return None

        except ImportError:
            logger.error("openai 库未安装，请运行: uv pip install openai")
            return None
        except Exception as e:
            logger.error(f"AI API 调用失败 ({self.provider}): {str(e)[:200]}")
            return None


class AIFilter:
    def __init__(self):
        self.storage = NewsStorage()
        self.client = AIClient()
        self.source_priorities = self._build_source_priorities()

    def _build_source_priorities(self) -> Dict[str, int]:
        priorities = {}
        for source in settings.news_sources:
            priorities[source['name']] = source.get('priority', 1)
        return priorities

    def _candidate_pool_size(self, max_news: int) -> int:
        multiplier = max(int(settings.filter_settings.get('candidate_pool_multiplier', 3)), 1)
        return max_news * multiplier

    @staticmethod
    def _candidate_identity(news: Dict[str, Any]) -> Any:
        return news.get('id') or news.get('link') or id(news)

    def _candidate_rank(self, news: Dict[str, Any]) -> tuple:
        return (
            float(news.get('relevance_score', 0) or 0),
            float(news.get('novelty_score', 0) or 0),
            float(news.get('impact_score', 0) or 0),
            self.source_priorities.get(news.get('source', ''), 1),
        )

    def _unique_candidates(self, news_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        result = []
        seen = set()
        for news in news_list:
            identity = self._candidate_identity(news)
            if identity in seen:
                continue
            seen.add(identity)
            result.append(news)
        return result

    @staticmethod
    def _normalize_topic(topic: str) -> str:
        aliases = {
            '宏观政策': '政策监管', '政策监管': '政策监管', '宏观数据': '政策监管',
            '财经市场': '财经市场', '金融市场': '财经市场',
            '科技产业': '科技产业', '产业公司': '科技产业', '科技创新': '科技产业',
            '国际局势': '国际局势', '地缘政治': '国际局势',
            '其他': '其他', '非核心': '其他',
        }
        return aliases.get(str(topic or '').strip(), '')

    @staticmethod
    def _contains_keyword(text: str, keyword: str) -> bool:
        keyword = keyword.lower()
        if re.fullmatch(r'[a-z0-9 ]+', keyword):
            return re.search(rf'(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])', text) is not None
        return keyword in text

    def _infer_topic(self, news: Dict[str, Any]) -> str:
        text = (news.get('title', '') + ' ' + (news.get('ai_summary', '') or news.get('summary', '') or '')).lower()
        topic_keywords = {
            '政策监管': [
                'gdp', 'cpi', 'pmi', '就业', '失业', '消费', '出口', '进口', '经济增长',
                '政策', '监管', '央行', '美联储', '降息', '加息', '利率', '财政', '预算',
                '税收', '法规', '立法', '关税', '贸易规则', '审批', '审查', 'fed',
                'regulation', 'monetary policy', 'fiscal policy', 'tariff'
            ],
            '财经市场': [
                '股市', '指数', '期货', '油价', '汇率', '美元', '黄金', '债券', '收益率',
                '通胀', 'ipo', '信用评级', '银行', '基金', 'stock market', 'bond', 'yield',
                'oil price', 'foreign exchange', 'inflation', 'credit rating'
            ],
            '科技产业': [
                '芯片', '半导体', '人工智能', '科技', '产能', '供应链', '并购', '收购',
                '重组', '业绩', '营收', '利润', '融资', 'ceo', '公司', '企业', '制造',
                '汽车', '电动车', '新能源', '电池', '能源', '航天', '医药', '生物科技',
                'chip', 'semiconductor', 'artificial intelligence', 'technology', 'capacity',
                'supply chain', 'merger', 'acquisition', 'earnings', 'revenue', 'automotive',
                'electric vehicle', 'biotech', 'aerospace'
            ],
            '国际局势': [
                '战争', '冲突', '制裁', '停火', '军事', '袭击', '导弹', '货船', '伊朗',
                '以色列', '乌克兰', '俄罗斯', '中东', '台海', '南海', '美军', '北约',
                'war', 'conflict', 'sanction', 'ceasefire', 'military', 'attack', 'strike',
                'iran', 'israel', 'ukraine', 'russia', 'middle east'
            ],
        }
        for topic in ('政策监管', '财经市场', '科技产业', '国际局势'):
            if any(self._contains_keyword(text, kw) for kw in topic_keywords[topic]):
                return topic
        return '其他'

    @staticmethod
    def _normalize_key(value: str) -> str:
        value = re.sub(r'[^a-z0-9一-鿿_-]+', '-', str(value or '').lower()).strip('-')
        return value[:100]

    @staticmethod
    def _normalize_link(value: str) -> str:
        link = str(value or '').strip()
        if not link:
            return ''
        try:
            parts = urlsplit(link)
            scheme = (parts.scheme or 'https').lower()
            netloc = parts.netloc.lower()
            if netloc.startswith('www.'):
                netloc = netloc[4:]
            path = re.sub(r'/+', '/', parts.path or '/').rstrip('/') or '/'
            tracking_keys = {
                'at_campaign', 'at_medium', 'fbclid', 'gclid', 'output',
                'ref', 'traffic_source', 'mc_cid', 'mc_eid',
            }
            query_items = [
                (key, item_value)
                for key, item_value in parse_qsl(parts.query, keep_blank_values=True)
                if not key.lower().startswith('utm_') and key.lower() not in tracking_keys
            ]
            query = urlencode(sorted(query_items))
            return urlunsplit((scheme, netloc, path, query, ''))
        except Exception:
            return link.lower().rstrip('/')

    def _known_story_cluster(self, news: Dict[str, Any]) -> str:
        text = (news.get('title', '') + ' ' + (news.get('ai_summary', '') or news.get('summary', '') or '')).lower()
        clusters = [
            ('iran-conflict', ['伊朗', '美伊', '霍尔木兹', 'iran', 'hormuz']),
            ('israel-gaza', ['加沙', '哈马斯', 'gaza', 'hamas']),
            ('russia-ukraine', ['俄乌', '乌克兰', 'ukraine']),
            ('china-us-trade', ['中美贸易', '中美经贸', 'us-china trade', 'china-us trade']),
            ('taiwan-strait', ['台海', '对台军售', 'taiwan strait', 'taiwan arms']),
        ]
        for key, aliases in clusters:
            if any(alias in text for alias in aliases):
                return key
        return ''

    def _derive_story_key(self, news: Dict[str, Any], suggested: str = '') -> str:
        known = self._known_story_cluster(news)
        if known:
            return known
        normalized = self._normalize_key(suggested)
        if normalized:
            return normalized
        event_key = news.get('event_key') or self._generate_event_fingerprint(news)
        return self._normalize_key(event_key) or self._normalize_key(news.get('title', ''))

    def _generate_event_fingerprint(self, news: Dict[str, Any]) -> str:
        title = news.get('title', '')
        summary = (news.get('ai_summary', '') or news.get('summary', '') or '')[:200]
        text = f"{title} {summary}"

        words = re.findall(r'[\u4e00-\u9fff]{2,}|[a-zA-Z]{3,}', text)

        entity_patterns = [
            r'伊朗|以色列|美国|中国|日本|俄罗斯|乌克兰|欧盟|北约|东盟|印尼|泰国|韩国|朝鲜',
            r'革命卫队|美联储|央行|证监会|国防部|外交部|白宫|克里姆林宫',
            r'霍尔木兹海峡|南海|台海|红海|波罗的海',
            r'比特币|美元|黄金|原油|纳斯达克|恒生|A股|日经',
        ]
        action_patterns = [
            r'扣押|袭击|制裁|加息|降息|发布|宣布|签署|批准|退出|加入|起诉|调查'
        ]

        entities = []
        for pattern in entity_patterns:
            entities.extend(re.findall(pattern, text))

        actions = []
        for pattern in action_patterns:
            actions.extend(re.findall(pattern, text))

        # Combine key tokens into a stable fingerprint string for deduplication
        fingerprint_parts = words[:4] + entities[:3] + actions[:2]
        return "|".join(fingerprint_parts)

    def _filter_by_date(self, news_list: List[Dict[str, Any]], hours: int = 24) -> List[Dict[str, Any]]:
        from datetime import datetime, timezone, timedelta
        import dateutil.parser

        tz_cn = timezone(timedelta(hours=8))
        now = datetime.now(tz_cn)
        cutoff = now.timestamp() - (hours * 3600)

        filtered = []
        removed_details = []
        for news in news_list:
            pub = news.get('published', '')
            if not pub:
                removed_details.append(f"无日期: {news.get('title', '?')[:30]}")
                continue
            try:
                dt = dateutil.parser.parse(str(pub))
                if dt.tzinfo is None:
                    cat = news.get('category', '')
                    if news.get('_published_timezone') == 'Asia/Shanghai':
                        dt = dt.replace(tzinfo=tz_cn)
                    elif cat == '英文':
                        dt = dt.replace(tzinfo=timezone.utc)
                    else:
                        dt = dt.replace(tzinfo=tz_cn)
                age_hours = (now.timestamp() - dt.timestamp()) / 3600
                if dt.timestamp() >= cutoff:
                    filtered.append(news)
                else:
                    removed_details.append(f"超{age_hours:.0f}h: {news.get('title', '?')[:30]}")
            except Exception as e:
                logger.warning(f"日期解析失败: {pub} - {e}, 保留该新闻")
                filtered.append(news)

        if removed_details:
            logger.info(f"日期过滤移除 {len(removed_details)} 条: {removed_details[:5]}")
        logger.info(f"日期过滤: {len(news_list)} -> {len(filtered)} 条 (保留{hours}小时内)")
        return filtered

    def filter_news(self, news_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not news_list:
            return []

        dated_news = self._filter_by_date(news_list)
        rules = self._load_filter_rules()
        if not dated_news:
            return []

        if not self.client.is_configured():
            logger.warning("未配置 AI API Key，使用统一关键词评估与组合选稿")
            coarse_filtered = self._coarse_filter(dated_news, rules)
            candidates = self._keyword_filter(coarse_filtered, rules)
            return self._finalize_candidates(candidates, [], rules.get('max_news', 20), use_ai_dedup=False)

        return self._two_round_filter(dated_news, rules)

    async def filter_news_async(self, news_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not news_list:
            return []

        dated_news = self._filter_by_date(news_list)
        rules = self._load_filter_rules()
        if not dated_news:
            return []

        if not self.client.is_configured():
            logger.warning("未配置 AI API Key，使用统一关键词评估与组合选稿")
            coarse_filtered = self._coarse_filter(dated_news, rules)
            candidates = self._keyword_filter(coarse_filtered, rules)
            return self._finalize_candidates(candidates, [], rules.get('max_news', 20), use_ai_dedup=False)

        return await self._two_round_filter_async(dated_news, rules)

    def _load_filter_rules(self) -> Dict[str, Any]:
        rules = {
            'include': [],
            'exclude': [],
            'max_news': settings.filter_settings.get('max_news', 20)
        }

        try:
            import sqlite3
            with sqlite3.connect(self.storage.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM filter_rules ORDER BY priority DESC')
                for row in cursor.fetchall():
                    rule = {
                        'id': row['id'],
                        'name': row['name'],
                        'type': row['type'],
                        'value': row['value'],
                        'priority': row['priority']
                    }
                    if row['type'] == 'include':
                        rules['include'].append(rule)
                    elif row['type'] == 'exclude':
                        rules['exclude'].append(rule)

            if not rules['include'] and not rules['exclude']:
                rules['include'] = [
                    {'name': kw, 'type': 'include', 'value': kw, 'priority': 1}
                    for kw in settings.filter_settings.get('keywords', [])
                ]
                rules['exclude'] = [
                    {'name': kw, 'type': 'exclude', 'value': kw, 'priority': 1}
                    for kw in settings.filter_settings.get('exclude_keywords', [])
                ]

        except Exception as e:
            logger.error(f"加载筛选规则时出错: {str(e)}")
            rules['include'] = [
                {'name': kw, 'type': 'include', 'value': kw, 'priority': 1}
                for kw in settings.filter_settings.get('keywords', [])
            ]
            rules['exclude'] = [
                {'name': kw, 'type': 'exclude', 'value': kw, 'priority': 1}
                for kw in settings.filter_settings.get('exclude_keywords', [])
            ]

        return rules

    def _keyword_filter(self, news_list: List[Dict[str, Any]], rules: Dict[str, Any]) -> List[Dict[str, Any]]:
        eligible_news = self._coarse_filter(news_list, rules)
        candidates = []
        include_rules = rules.get('include', [])

        for news in eligible_news:
            title = news.get('title', '')
            summary = news.get('summary', '') or ''
            text = f"{title} {summary}".lower()
            score = sum(
                rule.get('priority', 1)
                for rule in include_rules
                if str(rule.get('value', '')).lower() in text
            )
            if score <= 0:
                continue

            news['relevance_score'] = max(float(news.get('relevance_score', 0) or 0), float(score))
            news['impact_score'] = float(news.get('impact_score', score) or score)
            news['novelty_score'] = float(news.get('novelty_score', score) or score)
            news['topic'] = self._normalize_topic(news.get('topic', '')) or self._infer_topic(news)
            news['event_key'] = self._normalize_key(news.get('event_key', '')) or self._normalize_key(
                self._generate_event_fingerprint(news)
            )
            news['story_key'] = self._derive_story_key(news, news.get('story_key', ''))
            news['filter_reason'] = news.get('filter_reason', '') or '关键词规则命中'
            if not news.get('ai_summary'):
                news['ai_summary'] = self._generate_simple_summary(news)
            candidates.append(news)

        candidates = self._unique_candidates(candidates)
        candidates.sort(key=self._candidate_rank, reverse=True)
        limit = self._candidate_pool_size(rules.get('max_news', 20))
        logger.info(f"关键词评估: {len(news_list)} -> {len(candidates[:limit])} 条候选")
        return candidates[:limit]

    def _generate_simple_summary(self, news: Dict[str, Any]) -> str:
        summary = (news.get('summary', '') or '').strip()
        if not summary:
            return news.get('title', '')[:50]

        numeric_clauses = [
            part.strip()
            for part in re.split(r'[。！？!?；;\n]+', summary)
            if _key_figure_numbers(part)
        ]
        if numeric_clauses:
            return _summary_with_key_figures(news, numeric_clauses[0])

        # 先用句号/感叹号/问号分割，再用中文逗号/分号分割
        # 对齐 newsletter._generate_simple_summary() 的分割逻辑
        for sep in ['。', '！', '？', '；', '，']:
            parts = summary.split(sep)
            for part in parts:
                part = part.strip()
                if len(part) > 8:
                    if len(part) > 60:
                        return part[:60] + '...'
                    return part

        return summary[:60] + '...'

    @staticmethod
    def _is_unsupported_third_party_opinion(news: Dict[str, Any]) -> bool:
        text = f"{news.get('title', '')} {news.get('summary', '') or ''}"
        is_person_opinion = (
            THIRD_PARTY_OPINION_RE.search(text) is not None
            and OPINION_LANGUAGE_RE.search(text) is not None
        )
        is_vague_research_claim = VAGUE_RESEARCH_CLAIM_RE.search(text) is not None
        if not is_person_opinion and not is_vague_research_claim:
            return False
        evidence_text = VAGUE_RESEARCH_CLAIM_RE.sub('', text)
        if FACTUAL_ACTION_RE.search(evidence_text):
            return False
        if is_person_opinion:
            return True
        return KEY_FIGURE_RE.search(text) is None

    def _refill_after_dedup(
        self,
        deduplicated: List[Dict[str, Any]],
        reserve: List[Dict[str, Any]],
        max_news: int,
    ) -> List[Dict[str, Any]]:
        min_news = min(max(int(settings.filter_settings.get('min_news', max_news)), 1), max_news)
        if len(deduplicated) >= min_news or not reserve:
            return deduplicated

        existing_ids = {item.get('id') or item.get('link') or id(item) for item in deduplicated}
        reserve_pool = []
        for news in reserve:
            identity = news.get('id') or news.get('link') or id(news)
            if identity in existing_ids:
                continue
            if not news.get('ai_summary'):
                news['ai_summary'] = self._generate_simple_summary(news)
            news['filter_reason'] = (news.get('filter_reason', '') + '（去重后备用候选补位）').strip()
            reserve_pool.append(news)
            existing_ids.add(identity)
            if len(reserve_pool) >= self._candidate_pool_size(max_news):
                break

        if not reserve_pool:
            return deduplicated

        reserve_pool = self._event_deduplicate(reserve_pool)
        combined = list(deduplicated)
        seen_events = {self._normalize_key(item.get('event_key', '')) for item in combined}
        seen_links = {self._normalize_link(item.get('link', '')) for item in combined}
        seen_events.discard('')
        seen_links.discard('')

        def title_tokens(item: Dict[str, Any]) -> set:
            return set(re.findall(r'[\u4e00-\u9fff]|[a-zA-Z]+|\d+', item.get('title', '').lower()))

        seen_titles = [title_tokens(item) for item in combined if title_tokens(item)]
        added = 0
        for news in reserve_pool:
            event_key = self._normalize_key(news.get('event_key', ''))
            normalized_link = self._normalize_link(news.get('link', ''))
            if event_key and event_key in seen_events:
                continue
            if normalized_link and normalized_link in seen_links:
                continue

            tokens = title_tokens(news)
            is_title_duplicate = False
            if tokens:
                for existing_tokens in seen_titles:
                    union = len(tokens | existing_tokens)
                    if union and len(tokens & existing_tokens) / union > 0.6:
                        is_title_duplicate = True
                        break
            if is_title_duplicate:
                logger.info(f"备用候选标题去重-跳过: {news.get('title', '')[:40]}")
                continue

            combined.append(news)
            added += 1
            if event_key:
                seen_events.add(event_key)
            if normalized_link:
                seen_links.add(normalized_link)
            if tokens:
                seen_titles.append(tokens)
            if len(combined) >= max_news:
                break

        logger.info(
            f"去重后补位: {len(deduplicated)} + {added} -> {len(combined)} 条"
        )
        return combined

    def _finalize_candidates(
        self,
        candidates: List[Dict[str, Any]],
        reserve: List[Dict[str, Any]],
        max_news: int,
        use_ai_dedup: bool,
    ) -> List[Dict[str, Any]]:
        candidates = self._unique_candidates(candidates)
        reserve = self._unique_candidates(reserve)
        logger.info(f"候选去重阶段: 主候选={len(candidates)}, 备用={len(reserve)}")

        if use_ai_dedup and self.client.provider not in ('groq', 'qwen'):
            deduplicated = self._deduplicate_similar(candidates)
        else:
            deduplicated = self._title_deduplicate(candidates)
        deduplicated = self._event_deduplicate(deduplicated)
        deduplicated = self._refill_after_dedup(deduplicated, reserve, max_news)

        result = self._categorize_by_topic(deduplicated, max_news=max_news)
        logger.info(f"统一组合选稿完成: {len(result)} 条新闻")
        return result

    async def _finalize_candidates_async(
        self,
        candidates: List[Dict[str, Any]],
        reserve: List[Dict[str, Any]],
        max_news: int,
        use_ai_dedup: bool,
    ) -> List[Dict[str, Any]]:
        candidates = self._unique_candidates(candidates)
        reserve = self._unique_candidates(reserve)
        logger.info(f"候选去重阶段: 主候选={len(candidates)}, 备用={len(reserve)}")

        if use_ai_dedup and self.client.provider not in ('groq', 'qwen'):
            deduplicated = await self._deduplicate_similar_async(candidates)
        else:
            deduplicated = self._title_deduplicate(candidates)
        deduplicated = self._event_deduplicate(deduplicated)
        deduplicated = self._refill_after_dedup(deduplicated, reserve, max_news)

        result = self._categorize_by_topic(deduplicated, max_news=max_news)
        logger.info(f"统一组合选稿完成: {len(result)} 条新闻")
        return result

    def _two_round_filter(self, news_list: List[Dict[str, Any]], rules: Dict[str, Any]) -> List[Dict[str, Any]]:
        max_news = rules.get('max_news', 20)
        logger.info(f"统一粗筛: {len(news_list)} 条新闻")
        coarse_filtered = self._coarse_filter(news_list, rules)
        logger.info(f"粗筛后: {len(coarse_filtered)} 条新闻")

        candidates, reserve = self._ai_semantic_filter(coarse_filtered, rules)
        logger.info(f"AI评估后: 主候选={len(candidates)}, 备用={len(reserve)}")
        return self._finalize_candidates(candidates, reserve, max_news, use_ai_dedup=True)

    async def _two_round_filter_async(self, news_list: List[Dict[str, Any]], rules: Dict[str, Any]) -> List[Dict[str, Any]]:
        max_news = rules.get('max_news', 20)
        logger.info(f"统一粗筛: {len(news_list)} 条新闻")
        coarse_filtered = self._coarse_filter(news_list, rules)
        logger.info(f"粗筛后: {len(coarse_filtered)} 条新闻")

        candidates, reserve = await self._ai_semantic_filter_async(coarse_filtered, rules)
        logger.info(f"AI评估后: 主候选={len(candidates)}, 备用={len(reserve)}")
        return await self._finalize_candidates_async(candidates, reserve, max_news, use_ai_dedup=True)

    def _build_dedup_prompt(self, news_list: List[Dict[str, Any]]) -> str:
        news_items = []
        for i, news in enumerate(news_list):
            title = news.get('title', '')
            summary = news.get('ai_summary', '') or (news.get('summary', '') or '')[:100]
            score = news.get('relevance_score', 5)
            event_key = news.get('event_key', '')
            source = news.get('source', '')
            news_items.append(
                f"[{i}] 标题: {title}\n来源: {source}\n事件键: {event_key}\n"
                f"摘要: {summary[:140]}\n评分: {score}"
            )

        return f"""请识别以下新闻中的重复/相似新闻，保留最重要的那条。

判断标准：
- 只有明确属于同一具体事件、同一公司同一动作或同一链接的不同报道，才视为重复
- 不要因为行业、主题、数据类型或报道格式相似就合并；不同公司的业绩预告、回购、融资、产能和产品发布必须分别保留
- 同一公司但发布时间、行动或具体数据不同，也不要仅凭标题相似合并，除非可以确认是同一事件更新
- 跨来源、跨语言的同一次表决、裁决或政策决定也属于同一具体事件；事件键措辞不同不代表不同事件，应同时核对摘要中的主体、动作和对象
- 保留评分最高的；如果评分相同，保留信息最完整的
- 只有高置信度重复才移除，无法确认时保留全部条目

每一条被移除的新闻都必须在duplicates中明确指出它与哪一条保留新闻属于同一具体事件；仅列removed而没有对应关系不算高置信度依据。

请以JSON格式返回：
{{
  "keep_indices": [0, 2],
  "removed": [1],
  "duplicates": [
    {{"remove": 1, "keep": 0, "reason": "同一公司同一裁决"}}
  ]
}}

新闻列表：

{chr(10).join(news_items)}"""

    @staticmethod
    def _title_tokens(title: str) -> set:
        return set(re.findall(r'[\u4e00-\u9fff]|[a-zA-Z]+|\d+', str(title or '').lower()))

    def _has_high_confidence_duplicate(
        self,
        candidate: Dict[str, Any],
        kept: List[Dict[str, Any]],
    ) -> bool:
        candidate_event = self._normalize_key(candidate.get('event_key', ''))
        candidate_link = self._normalize_link(candidate.get('link', ''))
        candidate_tokens = self._title_tokens(candidate.get('title', ''))

        for existing in kept:
            existing_event = self._normalize_key(existing.get('event_key', ''))
            existing_link = self._normalize_link(existing.get('link', ''))
            if candidate_event and candidate_event == existing_event:
                return True
            if candidate_link and candidate_link == existing_link:
                return True

            existing_tokens = self._title_tokens(existing.get('title', ''))
            if not candidate_tokens or not existing_tokens:
                continue
            union = len(candidate_tokens | existing_tokens)
            if union and len(candidate_tokens & existing_tokens) / union >= 0.8:
                return True

        return False

    @staticmethod
    def _dedup_content_tokens(news: Dict[str, Any]) -> set[str]:
        text = f"{news.get('title', '')} {news.get('ai_summary', '') or news.get('summary', '') or ''}".lower()
        latin_stopwords = {
            'about', 'after', 'again', 'against', 'from', 'into', 'more', 'over',
            'says', 'that', 'their', 'this', 'with', 'would', 'news', 'report',
        }
        tokens = {
            token for token in re.findall(r'[a-z0-9]+', text)
            if len(token) >= 3 and token not in latin_stopwords
        }
        for segment in re.findall(r'[\u4e00-\u9fff]+', text):
            cleaned = re.sub(r'(?:表示|报道|消息|可能|影响|认为|预计)', '', segment)
            tokens.update(cleaned[i:i + 2] for i in range(max(len(cleaned) - 1, 0)))
        return tokens

    def _has_mapped_duplicate_evidence(
        self,
        candidate: Dict[str, Any],
        existing: Dict[str, Any],
    ) -> bool:
        if self._has_high_confidence_duplicate(candidate, [existing]):
            return True

        candidate_title = str(candidate.get('title', '') or '')
        existing_title = str(existing.get('title', '') or '')
        candidate_subject = re.split(r'[:：]', candidate_title, maxsplit=1)[0].strip().lower()
        existing_subject = re.split(r'[:：]', existing_title, maxsplit=1)[0].strip().lower()
        if ':' in candidate_title or '：' in candidate_title:
            if (':' in existing_title or '：' in existing_title) and candidate_subject != existing_subject:
                return False

        candidate_numbers = _key_figure_numbers(
            f"{candidate_title} {candidate.get('ai_summary', '') or candidate.get('summary', '') or ''}"
        )
        existing_numbers = _key_figure_numbers(
            f"{existing_title} {existing.get('ai_summary', '') or existing.get('summary', '') or ''}"
        )
        if candidate_numbers and existing_numbers and not (candidate_numbers & existing_numbers):
            return False

        candidate_tokens = self._dedup_content_tokens(candidate)
        existing_tokens = self._dedup_content_tokens(existing)
        shared = candidate_tokens & existing_tokens
        union = candidate_tokens | existing_tokens
        if len(shared) >= 4 and union and len(shared) / len(union) >= 0.24:
            return True

        # 原文标题和中文摘要混用时，标题词元会稀释同一事件的摘要重合度。
        # 仅在 AI 已显式映射为同一事件后，单独复核摘要；不用于无映射自动合并。
        candidate_summary = self._dedup_content_tokens({
            'summary': candidate.get('ai_summary', '') or candidate.get('summary', '') or '',
        })
        existing_summary = self._dedup_content_tokens({
            'summary': existing.get('ai_summary', '') or existing.get('summary', '') or '',
        })
        summary_shared = candidate_summary & existing_summary
        summary_union = candidate_summary | existing_summary
        shorter = min(len(candidate_summary), len(existing_summary))
        if (
            len(summary_shared) >= 8
            and len(summary_shared) / len(summary_union) >= 0.2
            and shorter and len(summary_shared) / shorter >= 0.45
        ):
            return True

        normalized_candidate = re.sub(r'[^a-z0-9\u4e00-\u9fff]+', '', candidate_title.lower())
        normalized_existing = re.sub(r'[^a-z0-9\u4e00-\u9fff]+', '', existing_title.lower())
        return (
            len(shared) >= 3
            and min(len(normalized_candidate), len(normalized_existing)) >= 12
            and SequenceMatcher(None, normalized_candidate, normalized_existing).ratio() >= 0.55
        )

    def _parse_dedup_response(
        self,
        news_list: List[Dict[str, Any]],
        result_text: Optional[str],
    ) -> List[Dict[str, Any]]:
        if not result_text:
            logger.warning("AI去重返回空结果，使用标题相似度去重")
            return self._title_deduplicate(news_list)

        result = _extract_json(result_text)
        keep_indices = result.get('keep_indices', []) if result else []
        normalized_indices = []
        for value in keep_indices:
            try:
                index = int(value)
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(news_list) and index not in normalized_indices:
                normalized_indices.append(index)

        if not normalized_indices:
            logger.warning("AI去重没有返回有效索引，使用标题相似度去重")
            return self._title_deduplicate(news_list)

        kept_news = [news_list[i] for i in normalized_indices]
        removed_indices = [i for i in range(len(news_list)) if i not in normalized_indices]
        mapped_duplicates = set()
        duplicate_rows = result.get('duplicates', []) if result else []
        if isinstance(duplicate_rows, list):
            for row in duplicate_rows:
                if not isinstance(row, dict):
                    continue
                try:
                    remove_index = int(row.get('remove'))
                    keep_index = int(row.get('keep'))
                except (TypeError, ValueError):
                    continue
                if (
                    remove_index in removed_indices
                    and keep_index in normalized_indices
                    and self._has_mapped_duplicate_evidence(
                        news_list[remove_index], news_list[keep_index]
                    )
                ):
                    mapped_duplicates.add(remove_index)
        restored = [
            news_list[i]
            for i in removed_indices
            if i not in mapped_duplicates
            and not self._has_high_confidence_duplicate(news_list[i], kept_news)
        ]
        if restored:
            logger.info(f"AI去重保护性恢复 {len(restored)} 条低置信度移除项")
        removed_titles = [
            f"{i}:{news_list[i].get('title', '')[:30]}"
            for i in removed_indices
            if news_list[i] not in restored
        ]
        logger.info(
            f"去重: 从{len(news_list)}条中移除{len(removed_titles)}条重复, "
            f"移除明细={removed_titles}"
        )
        return kept_news + restored

    def _deduplicate_similar(self, news_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if len(news_list) <= 1:
            return news_list
        result_text = self.client.chat(
            system_prompt="你是一个新闻去重助手，只返回JSON格式结果。",
            user_prompt=self._build_dedup_prompt(news_list),
            json_mode=True,
        )
        return self._parse_dedup_response(news_list, result_text)

    async def _deduplicate_similar_async(self, news_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if len(news_list) <= 1:
            return news_list
        result_text = await self.client.chat_async(
            system_prompt="你是一个新闻去重助手，只返回JSON格式结果。",
            user_prompt=self._build_dedup_prompt(news_list),
            json_mode=True,
        )
        return self._parse_dedup_response(news_list, result_text)

    def _title_deduplicate(self, news_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if len(news_list) <= 1:
            return news_list

        def _tokenize(text: str) -> set:
            return self._title_tokens(text)

        seen_tokens = []
        result = []
        for news in news_list:
            title = news.get('title', '').strip()
            title_tokens = _tokenize(title)
            if len(title_tokens) < 2:
                result.append(news)
                continue
            is_dup = False
            for existing_tokens in seen_tokens:
                intersection = len(title_tokens & existing_tokens)
                union = len(title_tokens | existing_tokens)
                if union > 0 and intersection / union > 0.6:
                    is_dup = True
                    break
            if not is_dup:
                seen_tokens.append(title_tokens)
                result.append(news)
            else:
                logger.info(f"标题去重移除: {title[:40]}")

        logger.info(f"标题去重: {len(news_list)} -> {len(result)} 条")
        return result

    def _event_deduplicate(self, news_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        dedup_window_hours = settings.filter_settings.get('dedup_window_hours', 72)
        recent_events = self.storage.get_recent_events(days=max(dedup_window_hours // 24, 1))
        recent_links = {
            self._normalize_link(item.get('link', '')): item
            for item in recent_events.values()
            if self._normalize_link(item.get('link', ''))
        }
        logger.info(
            f"加载已发布事件记忆: {len(recent_events)} 条, 规范化链接: {len(recent_links)} 条"
        )

        seen_events: Dict[str, Dict[str, Any]] = {}
        result: List[Dict[str, Any]] = []

        for news in news_list:
            normalized_link = self._normalize_link(news.get('link', ''))
            if normalized_link and normalized_link in recent_links:
                kept = recent_links[normalized_link]
                logger.info(
                    f"跨简报链接去重-跳过: [{news.get('source', '')}:{news.get('title', '')[:30]}] "
                    f"(已发布: {kept.get('source', '')})"
                )
                continue

            event_key = self._normalize_key(news.get('event_key', ''))
            if not event_key:
                event_key = self._normalize_key(self._generate_event_fingerprint(news))
            news['event_key'] = event_key
            news['story_key'] = self._derive_story_key(news, news.get('story_key', ''))

            if not event_key:
                result.append(news)
                continue

            if event_key in recent_events:
                kept = recent_events[event_key]
                logger.info(
                    f"跨天事件去重-跳过: [{news.get('source', '')}:{news.get('title', '')[:30]}] "
                    f"(已发布: {kept.get('source', '')})"
                )
                continue

            source = news.get('source', '')
            quality = (
                news.get('relevance_score', 0),
                news.get('novelty_score', 0),
                self.source_priorities.get(source, 1),
                len(news.get('ai_summary', '') or news.get('summary', '') or ''),
            )
            existing = seen_events.get(event_key)
            if existing is None:
                seen_events[event_key] = {'news': news, 'quality': quality}
                result.append(news)
                continue

            if quality > existing['quality']:
                old_news = existing['news']
                result = [item for item in result if item is not old_news]
                result.append(news)
                seen_events[event_key] = {'news': news, 'quality': quality}
                logger.info(f"当日事件去重-替换: {old_news.get('title', '')[:30]} -> {news.get('title', '')[:30]}")
            else:
                logger.info(f"当日事件去重-跳过: {news.get('title', '')[:40]}")

        logger.info(f"事件去重: {len(news_list)} -> {len(result)} 条")
        return result

    def _coarse_filter(self, news_list: List[Dict[str, Any]], rules: Dict[str, Any]) -> List[Dict[str, Any]]:
        filtered = []
        exclude_keywords = [r['value'].lower() for r in rules.get('exclude', [])]
        all_exclude = list(set(exclude_keywords + DEFAULT_EXCLUDE_KEYWORDS))

        for news in news_list:
            title = news.get('title', '')
            summary = news.get('summary', '') or ''
            text = f"{title} {summary}".lower()

            if self._is_unsupported_third_party_opinion(news):
                logger.info(f"粗筛排除无事实支撑的第三方观点: {title[:50]}")
                news['_hard_excluded'] = True
                continue

            excluded = False
            for kw in all_exclude:
                if kw.lower() in text:
                    excluded = True
                    break
            if excluded:
                continue

            if not title or len(title) <= 5:
                continue

            filtered.append(news)

        return filtered

    def _prepare_unresolved_fallback(
        self,
        unresolved: List[Dict[str, Any]],
        rules: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        if not unresolved:
            return []

        fallback_rules = {
            **rules,
            'max_news': min(self._candidate_pool_size(rules.get('max_news', 20)), len(unresolved)),
        }
        fallback = self._keyword_filter(unresolved, fallback_rules)
        for news in fallback:
            news['topic'] = self._normalize_topic(news.get('topic', '')) or self._infer_topic(news)
            news['event_key'] = self._normalize_key(news.get('event_key', '')) or self._normalize_key(
                self._generate_event_fingerprint(news)
            )
            news['story_key'] = self._derive_story_key(news, news.get('story_key', ''))
            news['filter_reason'] = 'AI结构化响应失败，关键词回退候选'
            news['_ai_fallback'] = True
        logger.warning(f"AI结构化失败项使用关键词回退: {len(unresolved)} -> {len(fallback)} 条候选")
        return fallback

    def _build_ai_reserve(
        self,
        news_list: List[Dict[str, Any]],
        selected: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        selected_ids = {item.get('id') or item.get('link') or id(item) for item in selected}
        min_score = float(settings.filter_settings.get('ai_reserve_score_min', 4))
        reserve = []
        for news in news_list:
            identity = news.get('id') or news.get('link') or id(news)
            if identity in selected_ids or not news.get('_ai_assessed'):
                continue
            if news.get('_hard_excluded'):
                continue
            if news.get('topic') == '其他' or float(news.get('relevance_score', 0) or 0) < min_score:
                continue
            reserve.append(news)

        reserve.sort(
            key=lambda item: (
                item.get('relevance_score', 0),
                item.get('impact_score', 0),
                item.get('novelty_score', 0),
                self.source_priorities.get(item.get('source', ''), 1),
            ),
            reverse=True,
        )
        return reserve

    def _finalize_ai_assessment(
        self,
        news_list: List[Dict[str, Any]],
        selected: List[Dict[str, Any]],
        unresolved: List[Dict[str, Any]],
        rules: Dict[str, Any],
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        if unresolved:
            selected.extend(self._prepare_unresolved_fallback(unresolved, rules))

        selected = self._unique_candidates(selected)
        reserve = self._build_ai_reserve(news_list, selected)
        selected.sort(key=self._candidate_rank, reverse=True)
        reserve.sort(key=self._candidate_rank, reverse=True)
        limit = self._candidate_pool_size(rules.get('max_news', 20))
        return selected[:limit], reserve[:limit]

    def _ai_semantic_filter(
        self,
        news_list: List[Dict[str, Any]],
        rules: Dict[str, Any],
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        batch_size = self.client.batch_size
        selected = []
        unresolved = []

        for i in range(0, len(news_list), batch_size):
            batch = news_list[i:i + batch_size]
            logger.info(f"AI筛选批次 {i//batch_size + 1}/{(len(news_list)-1)//batch_size + 1}: {len(batch)} 条")
            batch_results, batch_unresolved = self._ai_filter_batch(batch)
            selected.extend(batch_results)
            unresolved.extend(batch_unresolved)
            if i + batch_size < len(news_list):
                logger.info(f"批次间等待 {self.client.request_delay} 秒...")
                time.sleep(self.client.request_delay)

        return self._finalize_ai_assessment(news_list, selected, unresolved, rules)

    async def _ai_semantic_filter_async(
        self,
        news_list: List[Dict[str, Any]],
        rules: Dict[str, Any],
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        batch_size = self.client.batch_size
        selected = []
        unresolved = []

        for i in range(0, len(news_list), batch_size):
            batch = news_list[i:i + batch_size]
            logger.info(f"AI筛选批次 {i//batch_size + 1}/{(len(news_list)-1)//batch_size + 1}: {len(batch)} 条")
            batch_results, batch_unresolved = await self._ai_filter_batch_async(batch)
            selected.extend(batch_results)
            unresolved.extend(batch_unresolved)
            if i + batch_size < len(news_list):
                logger.info(f"批次间等待 {self.client.request_delay} 秒...")
                await asyncio.sleep(self.client.request_delay)

        return self._finalize_ai_assessment(news_list, selected, unresolved, rules)

    def _build_filter_prompt(self, news_batch: List[Dict[str, Any]]) -> str:
        news_items = []
        for i, news in enumerate(news_batch):
            title = news.get('title', '')
            summary = (news.get('summary', '') or '')[:300]
            source = news.get('source', '')
            category = news.get('category', '')
            news_items.append(f"[{i}] 标题: {title}\n来源: {source} | 来源分类: {category}\n摘要: {summary}")


        return f"""请逐条评估以下新闻。财经、宏观、产业和科技是核心；地缘政治只在重大升级或具有明确市场传导时保留。

每条必须返回：
- keep：1保留，0排除
- score：综合分1-10
- impact_score：市场/经济/产业影响1-10
- novelty_score：相对近期报道的信息增量1-10
- category：政策监管、财经市场、科技产业、国际局势、其他之一
- story_key：同一长期故事共享的稳定短标识
- event_key：同一具体事件共享的稳定短标识
- reason：保留或排除理由
- summary：一句中文事实摘要，目标40-60字、最多80字（含标点）；输出前检查字数，超长时删减次要背景并重新组织为完整句子，不使用省略号；中文源也必须重新概括，保留核心主体、行动、结果及最重要的金额、比例、利率等数字，不照抄长正文、不罗列背景

输出压缩要求：
- 必须覆盖新闻列表中的每一个索引，不能省略被排除项
- keep=0 时 reason 不超过15个汉字，summary 返回空字符串
- story_key 和 event_key 使用不超过40字符的稳定短标识
- 不要复述标题，不要使用 Markdown 代码块

约束：
1. 普通战况、口头威胁、采访角度或实时滚动更新，没有重大升级时最高5分。
2. 纯观点或没有新数据、新政策、新公司行动的文章应排除或降至4分以下。
3. 普通券商、分析师、策略师、研究员或经济学家的预测和评论，若没有可核验数据或新增事实，keep必须为0；预测中的目标价、涨跌幅或点位仍是观点，不算事实数据。央行行长、美联储主席等有政策决定权者的正式政策信号除外。
4. 公司产能、并购、业绩、供应链、融资和重大技术进展应优先于重复地缘报道。
5. 不要因为来源是国际大媒体就自动加分。
6. summary不得添加“可能影响……”“或将提振……”“可能造成冲击……”等推断性尾缀；影响判断只写入impact_score和reason，不写入展示摘要。

严格返回：
{{
  "results": {{
    "0": {{"keep": 1, "score": 8, "impact_score": 8, "novelty_score": 9, "category": "科技产业", "story_key": "global-auto-capacity", "event_key": "company-cuts-one-million-capacity", "reason": "包含明确产能调整", "summary": "某汽车集团宣布削减100万辆产能"}}
  }}
}}

新闻列表：

{chr(10).join(news_items)}"""

    def _filter_request_max_tokens(self) -> int:
        return max(int(settings.filter_settings.get('ai_filter_max_tokens', 4000)), 1000)

    def _parse_filter_result(
        self,
        news_batch: List[Dict[str, Any]],
        result_text: str,
    ) -> Optional[List[Dict[str, Any]]]:
        result = _extract_json(result_text) if result_text else None
        if not result:
            logger.warning(f"AI筛选结果解析失败，响应前200字符: {(result_text or '空')[:200]}")
            return None

        results = result.get('results')
        if not isinstance(results, dict):
            logger.warning(f"AI筛选结果无有效results字段，响应前200字符: {(result_text or '')[:200]}")
            return None

        missing = [str(i) for i in range(len(news_batch)) if str(i) not in results]
        if missing:
            logger.warning(f"AI筛选结果不完整，缺少索引: {missing[:10]}")
            return None

        filtered = []
        kept_count = 0
        for i, news in enumerate(news_batch):
            item_result = results.get(str(i), {})
            if not isinstance(item_result, dict):
                logger.warning(f"AI筛选索引 {i} 不是JSON对象")
                return None

            def score_value(name: str, default: float = 0) -> float:
                try:
                    return float(item_result.get(name, default) or default)
                except (TypeError, ValueError):
                    return float(default)

            score = score_value('score')
            impact_score = score_value('impact_score', score)
            novelty_score = score_value('novelty_score', score)
            keep = item_result.get('keep', 0) in (1, True, '1')

            news['relevance_score'] = score
            news['impact_score'] = impact_score
            news['novelty_score'] = novelty_score
            news['filter_reason'] = str(item_result.get('reason', '') or '')
            news['topic'] = self._normalize_topic(item_result.get('category', '')) or self._infer_topic(news)
            suggested_event = self._normalize_key(item_result.get('event_key', ''))
            news['event_key'] = suggested_event or self._normalize_key(self._generate_event_fingerprint(news))
            news['story_key'] = self._derive_story_key(news, item_result.get('story_key', ''))
            ai_summary = str(item_result.get('summary', '') or '').strip()
            if ai_summary:
                news['ai_summary'] = _summary_with_key_figures(news, ai_summary)
            if keep and self._is_unsupported_third_party_opinion(news):
                keep = False
                news['_hard_excluded'] = True
                news['filter_reason'] = '无事实支撑的第三方观点'
            news['_ai_assessed'] = True
            news['_ai_keep'] = keep and score >= 5

            if news['_ai_keep']:
                if not news.get('ai_summary'):
                    news['ai_summary'] = self._generate_simple_summary(news)
                filtered.append(news)
                kept_count += 1

        logger.info(f"AI筛选批次结果: {len(results)}条评分, 保留{kept_count}条")
        return filtered

    def _ai_filter_batch(
        self,
        news_batch: List[Dict[str, Any]],
        split_depth: int = 0,
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        prompt = self._build_filter_prompt(news_batch)
        max_tokens = self._filter_request_max_tokens()
        result_text = self.client.chat(
            system_prompt=FILTER_SYSTEM_PROMPT,
            user_prompt=prompt,
            max_tokens=max_tokens,
            json_mode=True,
        )
        if not result_text:
            logger.warning("AI筛选批次返回空结果，关闭 json_mode 重试")
            result_text = self.client.chat(
                system_prompt=FILTER_SYSTEM_PROMPT,
                user_prompt=prompt + "\n请务必只返回JSON。",
                max_tokens=max_tokens,
                json_mode=False,
            )

        parsed = self._parse_filter_result(news_batch, result_text)
        if parsed is not None:
            return parsed, []

        max_split_depth = max(int(settings.filter_settings.get('ai_filter_split_depth', 2)), 0)
        if len(news_batch) > 1 and split_depth < max_split_depth:
            midpoint = len(news_batch) // 2
            logger.warning(
                f"AI筛选批次JSON无效，拆分重试: {len(news_batch)} -> "
                f"{midpoint}+{len(news_batch) - midpoint} (depth={split_depth + 1})"
            )
            left_results, left_unresolved = self._ai_filter_batch(news_batch[:midpoint], split_depth + 1)
            right_results, right_unresolved = self._ai_filter_batch(news_batch[midpoint:], split_depth + 1)
            return left_results + right_results, left_unresolved + right_unresolved

        logger.error(f"AI筛选批次在拆分后仍无法解析，保留 {len(news_batch)} 条作为显式回退输入")
        return [], list(news_batch)

    async def _ai_filter_batch_async(
        self,
        news_batch: List[Dict[str, Any]],
        split_depth: int = 0,
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        prompt = self._build_filter_prompt(news_batch)
        max_tokens = self._filter_request_max_tokens()
        result_text = await self.client.chat_async(
            system_prompt=FILTER_SYSTEM_PROMPT,
            user_prompt=prompt,
            max_tokens=max_tokens,
            json_mode=True,
        )
        if not result_text:
            logger.warning("AI筛选批次返回空结果，关闭 json_mode 重试")
            result_text = await self.client.chat_async(
                system_prompt=FILTER_SYSTEM_PROMPT,
                user_prompt=prompt + "\n请务必只返回JSON。",
                max_tokens=max_tokens,
                json_mode=False,
            )

        parsed = self._parse_filter_result(news_batch, result_text)
        if parsed is not None:
            return parsed, []

        max_split_depth = max(int(settings.filter_settings.get('ai_filter_split_depth', 2)), 0)
        if len(news_batch) > 1 and split_depth < max_split_depth:
            midpoint = len(news_batch) // 2
            logger.warning(
                f"AI筛选批次JSON无效，拆分重试: {len(news_batch)} -> "
                f"{midpoint}+{len(news_batch) - midpoint} (depth={split_depth + 1})"
            )
            left_results, left_unresolved = await self._ai_filter_batch_async(
                news_batch[:midpoint], split_depth + 1
            )
            right_results, right_unresolved = await self._ai_filter_batch_async(
                news_batch[midpoint:], split_depth + 1
            )
            return left_results + right_results, left_unresolved + right_unresolved

        logger.error(f"AI筛选批次在拆分后仍无法解析，保留 {len(news_batch)} 条作为显式回退输入")
        return [], list(news_batch)

    def _categorize_by_topic(
        self,
        news_list: List[Dict[str, Any]],
        max_news: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        if max_news is None:
            max_news = self._load_filter_rules().get('max_news', 20)
        quotas = settings.filter_settings.get('topic_quotas', {})
        per_source_max = max(int(settings.filter_settings.get('per_source_max', 4)), 1)
        per_story_max = max(int(settings.filter_settings.get('per_story_max', 2)), 1)

        for news in news_list:
            news['topic'] = self._normalize_topic(news.get('topic', '')) or self._infer_topic(news)
            news['event_key'] = self._normalize_key(news.get('event_key', '')) or self._normalize_key(self._generate_event_fingerprint(news))
            news['story_key'] = self._derive_story_key(news, news.get('story_key', ''))

        candidates = sorted(news_list, key=self._candidate_rank, reverse=True)
        selected: List[Dict[str, Any]] = []
        selected_ids = set()
        source_counts: Dict[str, int] = {}
        story_counts: Dict[str, int] = {}
        topic_counts: Dict[str, int] = {}

        def add(news: Dict[str, Any], source_limit: int, enforce_topic_quota: bool) -> bool:
            identity = news.get('id') or news.get('link') or id(news)
            if identity in selected_ids:
                return False
            source = news.get('source', '未知')
            story = news.get('story_key', '')
            topic = news.get('topic', '其他')
            topic_quota = int(quotas.get(topic, max_news))
            if source_counts.get(source, 0) >= source_limit:
                return False
            if story and story_counts.get(story, 0) >= per_story_max:
                return False
            if topic == '国际局势' and topic_counts.get(topic, 0) >= topic_quota:
                return False
            if enforce_topic_quota and topic_counts.get(topic, 0) >= topic_quota:
                return False
            selected.append(news)
            selected_ids.add(identity)
            source_counts[source] = source_counts.get(source, 0) + 1
            if story:
                story_counts[story] = story_counts.get(story, 0) + 1
            topic_counts[topic] = topic_counts.get(topic, 0) + 1
            return True

        # 先按目标版面填充核心分类，避免高分地缘新闻占满名额。
        for topic in ('政策监管', '财经市场', '科技产业', '国际局势', '其他'):
            target = int(quotas.get(topic, 0))
            if target <= 0:
                continue
            for news in candidates:
                if news.get('topic') == topic:
                    add(news, per_source_max, True)
                if topic_counts.get(topic, 0) >= target or len(selected) >= max_news:
                    break

        # 核心分类不足时按总分补位；地缘政治、故事和来源上限仍然生效。
        for news in candidates:
            if len(selected) >= max_news:
                break
            add(news, per_source_max, False)

        # 若来源较少导致条目不足，仅小幅放宽来源上限，不放宽故事和地缘上限。
        if len(selected) < max_news:
            for news in candidates:
                if len(selected) >= max_news:
                    break
                add(news, per_source_max + 2, False)

        logger.info(
            f"组合选稿: {len(news_list)} -> {len(selected)} 条, "
            f"分类={topic_counts}, 来源={source_counts}"
        )
        return selected

class AITranslator:
    def __init__(self):
        self.client = AIClient()

    @staticmethod
    def _clean_translation_line(value: str) -> str:
        value = re.sub(r'^\s*\[\d+\]\s*', '', str(value or ''))
        value = re.sub(r'^\s*(?:标题|摘要|title|summary)\s*[:：]\s*', '', value, flags=re.IGNORECASE)
        return value.strip()

    def is_available(self) -> bool:
        return self.client.is_configured() and settings.ai_translate_enabled

    def translate_news(self, news_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not self.is_available():
            return news_list

        foreign_news = [n for n in news_list if n.get('category', '') == '英文' and not n.get('translated')]
        if not foreign_news:
            logger.info("无需翻译的新闻")
            return news_list

        logger.info(f"批量翻译 {len(foreign_news)} 条英文新闻")
        try:
            self._translate_batch_text(foreign_news)
        except Exception as e:
            logger.error(f"翻译过程出错: {e}")
        return news_list

    async def translate_news_async(self, news_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not self.is_available():
            return news_list

        foreign_news = [n for n in news_list if n.get('category', '') == '英文' and not n.get('translated')]
        if not foreign_news:
            logger.info("无需翻译的新闻")
            return news_list

        logger.info(f"批量翻译 {len(foreign_news)} 条英文新闻")
        try:
            await self._translate_batch_text_async(foreign_news)
        except Exception as e:
            logger.error(f"翻译过程出错: {e}")
        return news_list

    @staticmethod
    def _translation_summary(news: Dict[str, Any]) -> str:
        return str(news.get('ai_summary', '') or news.get('summary', '') or '')[:300]

    def _apply_translation(
        self,
        news: Dict[str, Any],
        translated_title: str,
        translated_summary: str = '',
    ) -> None:
        original_title = news.get('title', '')
        clean_title = self._clean_translation_line(translated_title)
        clean_summary = _strip_speculative_impact_tail(self._clean_translation_line(translated_summary))
        if clean_title:
            news['title_original'] = original_title
            news['title'] = clean_title
        existing_ai_summary = str(news.get('ai_summary', '') or '')
        if existing_ai_summary and re.search(r'[\u4e00-\u9fff]', existing_ai_summary):
            news['ai_summary'] = _summary_with_key_figures(news, existing_ai_summary)
        elif clean_summary:
            if existing_ai_summary:
                news['ai_summary_original'] = news.get('ai_summary', '')
                news['ai_summary'] = _summary_with_key_figures(news, clean_summary)
            else:
                news['summary_original'] = news.get('summary', '')
                news['summary'] = _summary_with_key_figures(news, clean_summary)
        news['translated'] = 1

    def _translate_batch_text(self, news_batch: List[Dict[str, Any]]) -> None:
        batch_size = 5
        for start in range(0, len(news_batch), batch_size):
            batch = news_batch[start:start + batch_size]
            logger.info(f"翻译批次 {start//batch_size + 1}/{(len(news_batch) + batch_size - 1)//batch_size}: {len(batch)} 条")
            items = []
            for i, news in enumerate(batch):
                title = news.get('title', '')
                summary = self._translation_summary(news)
                items.append(f"[{i}] Title: {title}\nSummary: {summary}")

            text = "\n\n".join(items)

            prompt = f"""Translate the following English news into Simplified Chinese. For each item, return EXACTLY two lines:
Line 1: Chinese title (concise, include key entities like country names, organizations)
Line 2: Chinese factual summary (one complete sentence, target 40-60 Chinese characters, at most 80 including punctuation; shorten secondary background before returning, never use ellipses; include who did what + confirmed result + every decision-relevant number/casualty. If Summary is already Chinese, repeat its facts faithfully. Do not add forecasts, market implications, or "may affect" tails)
Separate each item with a blank line.

{text}"""

            result_text = self.client.chat(
                system_prompt="You are a professional English-to-Chinese news translator. Preserve all verified facts and key numbers. Never add forecasts, implications, or speculative impact statements.",
                user_prompt=prompt,
                temperature=0.3,
                max_tokens=1500,
                json_mode=False
            )

            if not result_text:
                logger.warning(f"翻译批次返回空结果，共 {len(batch)} 条新闻未翻译")
                continue

            blocks = [b.strip() for b in result_text.strip().split('\n\n') if b.strip()]
            expected = len(batch)
            actual = len(blocks)
            if actual < expected:
                logger.warning(f"翻译解析块数量不匹配: 期望 {expected} 块, 实际 {actual} 块, 将逐条回退重试缺失项")

            for i, news in enumerate(batch):
                if i < len(blocks):
                    lines = [l.strip() for l in blocks[i].split('\n') if l.strip()]
                    if len(lines) >= 2:
                        self._apply_translation(news, lines[0], lines[1])
                    elif len(lines) == 1:
                        logger.warning(f"翻译批次缺少摘要，逐条回退: {news.get('title', '')[:60]}")
                        self._translate_single_fallback(news)
                    else:
                        logger.warning(f"翻译解析失败: 块 {i} 无有效行，来源={news.get('source')}, 标题={news.get('title', '')[:60]}")
                        self._translate_single_fallback(news)
                else:
                    logger.warning(f"翻译解析失败: 块 {i} 缺失，来源={news.get('source')}, 标题={news.get('title', '')[:60]}")
                    self._translate_single_fallback(news)

    def _translate_single_fallback(self, news: Dict[str, Any]) -> None:
        title = news.get('title', '')
        summary = self._translation_summary(news)
        prompt = f"""Translate the following English news into Simplified Chinese. Return EXACTLY two lines:
Line 1: Chinese title (concise, include key entities)
Line 2: Chinese factual summary (one complete sentence, target 40-60 Chinese characters, at most 80 including punctuation; shorten secondary background before returning, never use ellipses; include who did what + confirmed result + all key numbers; do not add forecasts or speculative impact tails)

Title: {title}
Summary: {summary}"""

        result_text = self.client.chat(
            system_prompt="You are a professional English-to-Chinese news translator. Preserve all verified facts and key numbers. Never add forecasts or speculative implications.",
            user_prompt=prompt,
            temperature=0.3,
            max_tokens=500,
            json_mode=False
        )

        if not result_text:
            logger.warning(f"逐条翻译回退失败(API返回空): {title[:60]}")
            return

        lines = [line.strip() for line in result_text.strip().split('\n') if line.strip()]
        if not lines:
            logger.warning(f"逐条翻译回退失败(无有效行): {title[:60]}")
            return

        self._apply_translation(news, lines[0], lines[1] if len(lines) >= 2 else '')
        logger.info(f"逐条翻译回退成功: {title[:60]} -> {lines[0][:60]}")

    async def _translate_batch_text_async(self, news_batch: List[Dict[str, Any]]) -> None:
        batch_size = 5
        for start in range(0, len(news_batch), batch_size):
            batch = news_batch[start:start + batch_size]
            logger.info(f"翻译批次 {start//batch_size + 1}/{(len(news_batch) + batch_size - 1)//batch_size}: {len(batch)} 条")
            items = []
            for i, news in enumerate(batch):
                title = news.get('title', '')
                summary = self._translation_summary(news)
                items.append(f"[{i}] Title: {title}\nSummary: {summary}")

            text = "\n\n".join(items)

            prompt = f"""Translate the following English news into Simplified Chinese. For each item, return EXACTLY two lines:
Line 1: Chinese title (concise, include key entities like country names, organizations)
Line 2: Chinese factual summary (one complete sentence, target 40-60 Chinese characters, at most 80 including punctuation; shorten secondary background before returning, never use ellipses; include who did what + confirmed result + every decision-relevant number/casualty. If Summary is already Chinese, repeat its facts faithfully. Do not add forecasts, market implications, or "may affect" tails)
Separate each item with a blank line.

{text}"""

            result_text = await self.client.chat_async(
                system_prompt="You are a professional English-to-Chinese news translator. Preserve all verified facts and key numbers. Never add forecasts, implications, or speculative impact statements.",
                user_prompt=prompt,
                temperature=0.3,
                max_tokens=1500,
                json_mode=False
            )

            if not result_text:
                logger.warning(f"翻译批次返回空结果，共 {len(batch)} 条新闻未翻译")
                continue

            blocks = [b.strip() for b in result_text.strip().split('\n\n') if b.strip()]
            expected = len(batch)
            actual = len(blocks)
            if actual < expected:
                logger.warning(f"翻译解析块数量不匹配: 期望 {expected} 块, 实际 {actual} 块, 将逐条回退重试缺失项")

            for i, news in enumerate(batch):
                if i < len(blocks):
                    lines = [l.strip() for l in blocks[i].split('\n') if l.strip()]
                    if len(lines) >= 2:
                        self._apply_translation(news, lines[0], lines[1])
                    elif len(lines) == 1:
                        logger.warning(f"翻译批次缺少摘要，逐条回退: {news.get('title', '')[:60]}")
                        self._translate_single_fallback(news)
                    else:
                        logger.warning(f"翻译解析失败: 块 {i} 无有效行，来源={news.get('source')}, 标题={news.get('title', '')[:60]}")
                        self._translate_single_fallback(news)
                else:
                    logger.warning(f"翻译解析失败: 块 {i} 缺失，来源={news.get('source')}, 标题={news.get('title', '')[:60]}")
                    self._translate_single_fallback(news)
