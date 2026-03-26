"""
消息解析模块

负责消息内容的解析、图片提取、事件重构和输入状态检测。
"""
import json
import re
from urllib.parse import parse_qs, urlparse
from typing import List, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

# 检查是否为 aiocqhttp 平台
try:
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
    IS_AIOCQHTTP = True
except ImportError:
    IS_AIOCQHTTP = False


class MessageParser:
    """消息解析器：提供消息文本/图片提取、事件重构、输入状态检测等功能"""

    _URL_KEY_HINTS = {"jumpurl", "qqdocurl", "url", "musicurl"}
    _SCHEME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9+\-.]*://")
    _HOST_PATH_PATTERN = re.compile(r"^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(/.*)?$")
    
    def __init__(self, image_component=None, plain_component=None):
        self._ImageComponent = image_component
        self._PlainComponent = plain_component

    def _safe_json_loads(self, value):
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except Exception:
            return value

    def _normalize_url(self, value: str) -> str:
        if not value:
            return ""
        url = value.strip()
        if not url:
            return ""
        if url.startswith("//"):
            return f"https:{url}"
        if self._SCHEME_PATTERN.match(url):
            return url
        if self._HOST_PATH_PATTERN.match(url):
            return f"https://{url}"
        return ""

    def _rule_xiaoheihe_bbs_share(self, host: str, path: str, query: dict) -> str:
        if host == "api.xiaoheihe.cn" and path == "/v3/bbs/app/api/web/share":
            link_id = (query.get("link_id") or [""])[0].strip()
            if link_id:
                return f"https://www.xiaoheihe.cn/app/bbs/link/{link_id}"
        return ""

    def _rule_xiaoheihe_game_share(self, host: str, path: str, query: dict) -> str:
        if host == "api.xiaoheihe.cn" and path == "/game/share_game_detail":
            appid = (query.get("appid") or [""])[0].strip()
            game_type = (query.get("game_type") or ["pc"])[0].strip().lower() or "pc"
            if appid:
                return f"https://www.xiaoheihe.cn/app/topic/game/{game_type}/{appid}"
        return ""

    def _rule_tieba_post_share(self, host: str, path: str, query: dict) -> str:
        if host in {"tieba.baidu.com", "www.tieba.baidu.com"}:
            matched = re.match(r"^/p/(\d+)", path)
            if matched:
                return f"https://tieba.baidu.com/p/{matched.group(1)}"
        return ""

    def _rule_ncm_song_share(self, host: str, path: str, query: dict) -> str:
        if host == "y.music.163.com" and path == "/m/song":
            song_id = (query.get("id") or [""])[0].strip()
            if song_id.isdigit():
                return f"https://music.163.com/#/song?id={song_id}"
        return ""

    def _is_wrapper_share_url(self, url: str) -> bool:
        """识别QQ卡片常见中转壳链接，避免优先输出不可解析链接。"""
        try:
            parsed = urlparse(url)
            host = (parsed.netloc or "").lower()
            path = parsed.path or ""
            return host in {"m.q.qq.com", "q.qq.com"} and path.startswith("/a/s/")
        except Exception:
            return False

    def _apply_share_url_rules(self, host: str, path: str, query: dict, fallback_url: str) -> str:
        rules = (
            self._rule_xiaoheihe_bbs_share,
            self._rule_xiaoheihe_game_share,
            self._rule_tieba_post_share,
            self._rule_ncm_song_share,
        )
        for rule in rules:
            result = rule(host, path, query)
            if result:
                return result
        return fallback_url

    def _canonicalize_known_share_url(self, url: str) -> str:
        """统一入口：将卡片分享链接规范化为更稳定、可打开的网页链接。"""
        try:
            parsed = urlparse(url)
            host = (parsed.netloc or "").lower()
            path = parsed.path or ""
            query = parse_qs(parsed.query)
            return self._apply_share_url_rules(host, path, query, url)
        except Exception:
            return url

    def _extract_urls_from_json_payload(self, payload) -> List[str]:
        payload = self._safe_json_loads(payload)
        if isinstance(payload, dict) and "data" in payload:
            payload["data"] = self._safe_json_loads(payload.get("data"))

        extracted: List[str] = []

        def walk(node):
            if isinstance(node, dict):
                for key, val in node.items():
                    key_lower = str(key).lower()
                    if key_lower in self._URL_KEY_HINTS and isinstance(val, str):
                        normalized = self._normalize_url(val)
                        if normalized:
                            extracted.append(self._canonicalize_known_share_url(normalized))
                    walk(val)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(payload)

        # 去重并保持原始顺序
        deduped = []
        seen = set()
        for u in extracted:
            if u not in seen:
                seen.add(u)
                deduped.append(u)

        non_wrappers = [u for u in deduped if not self._is_wrapper_share_url(u)]
        return non_wrappers or deduped

    def is_command(self, message: str, prefixes: list) -> bool:
        """检查消息是否为指令"""
        message = message.strip()
        if not message:
            return False
        for prefix in prefixes:
            if message.startswith(prefix):
                return True
        return False

    def is_typing_event(self, event: AstrMessageEvent) -> bool:
        """检测是否为输入状态通知事件（NapCat input_status）"""
        if not IS_AIOCQHTTP:
            return False
        try:
            raw = getattr(event.message_obj, 'raw_message', None)
            if raw is None:
                return False
            return (
                raw.get('post_type') == 'notice'
                and raw.get('sub_type') == 'input_status'
            )
        except Exception:
            return False

    def parse_message(self, message_obj) -> Tuple[str, bool, List[str]]:
        """
        解析消息对象，提取文本、图片和合并转发信息
        
        Returns:
            (文本内容, 是否包含图片, 图片URL列表)
        """
        text = ""
        has_image = False
        image_urls = []
        card_urls = []
        try:
            if not hasattr(message_obj, "message"):
                return "", False, []
            
            for component in message_obj.message:
                # 跳过Reply组件（引用消息由forward_handler单独处理）
                if component.__class__.__name__ == 'Reply':
                    continue
                
                # 提取文本内容（支持多种属性名）
                if hasattr(component, 'text') and component.text:
                    text += component.text
                elif hasattr(component, 'content') and component.content:
                    text += component.content

                # 提取QQ卡片(json段)中的原始链接
                json_payload = None
                if component.__class__.__name__ == 'Json' and hasattr(component, 'data'):
                    json_payload = component.data
                elif isinstance(component, dict) and component.get('type') == 'json':
                    json_payload = component.get('data')
                if json_payload is not None:
                    card_urls.extend(self._extract_urls_from_json_payload(json_payload))
                
                # 识别图片组件（优先使用 isinstance，后备使用类名检查）
                is_img = False
                if self._ImageComponent and isinstance(component, self._ImageComponent):
                    is_img = True
                elif component.__class__.__name__ == 'Image':
                    is_img = True
                
                # 提取图片URL
                if is_img:
                    has_image = True
                    if hasattr(component, 'url') and component.url:
                        image_urls.append(component.url)
                    elif hasattr(component, 'file') and component.file:
                        image_urls.append(component.file)
        except Exception:
            pass

        if card_urls:
            # 去重并添加标识，便于LLM在整合消息中识别原始来源链接
            deduped_links = []
            seen = set()
            for u in card_urls:
                if u not in seen:
                    seen.add(u)
                    deduped_links.append(u)
            card_text = "\n".join(f"[卡片链接] {u}" for u in deduped_links)
            text = (f"{text}\n{card_text}" if text else card_text)

        return text, has_image, image_urls

    def reconstruct_event(self, event: AstrMessageEvent, text: str, image_urls: List[str]):
        """
        重构消息事件，将合并后的文本和图片重新组装到事件对象中
        这样事件可以继续传播给后续的插件/框架处理
        """
        event.message_str = text
        if not self._PlainComponent:
            return

        # 构建消息组件链：文本 + 图片
        chain = []
        if text:
            chain.append(self._PlainComponent(text=text))
        
        # 添加图片组件（兼容不同的 Image 构造函数参数）
        if image_urls and self._ImageComponent:
            for url in image_urls:
                try:
                    chain.append(self._ImageComponent(file=url))
                except TypeError:
                    chain.append(self._ImageComponent(url=url))
                except Exception:
                    pass
        
        # 更新事件的消息对象
        if hasattr(event.message_obj, "message"):
            try:
                event.message_obj.message = chain
            except Exception:
                pass
