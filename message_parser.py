"""
消息解析模块

负责消息内容的解析、图片提取、事件重构和输入状态检测。
"""
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
    
    def __init__(self, image_component=None, plain_component=None):
        self._ImageComponent = image_component
        self._PlainComponent = plain_component

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
