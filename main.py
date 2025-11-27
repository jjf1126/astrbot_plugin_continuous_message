import asyncio
from typing import List, Tuple, Dict, Optional
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import AstrBotConfig, logger

@register(
    "continuous_message",
    "aliveriver",
    "消息防抖动插件（高性能事件驱动版）",
    "2.1.0"
)
class ContinuousMessagePlugin(Star):
    """
    消息防抖动插件 v2.1.0 - 高性能事件驱动版
    
    性能优化：
    1. 弃用 while 轮询，改用 asyncio.Event 挂起主会话，实现 0 CPU 占用等待。
    2. 使用 Task Cancel 机制实现精确的计时器重置。
    
    日志优化：
    1. 减少冗余输出，仅在关键节点记录日志。
    """
    
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        
        self.debounce_time = float(self.config.get('debounce_time', 2.0))
        self.command_prefixes = self.config.get('command_prefixes', ['/'])
        self.enable_plugin = self.config.get('enable', True)
        self.merge_separator = self.config.get('merge_separator', '\n')
        
        # 会话存储结构:
        # {
        #   uid: {
        #     'buffer': [],
        #     'images': [],
        #     'flush_event': asyncio.Event,  # 用于唤醒 Msg 1
        #     'timer_task': asyncio.Task     # 当前正在倒计时的任务
        #   }
        # }
        self.sessions: Dict[str, Dict] = {}
        
        self._ImageComponent = None
        self._PlainComponent = None
        
        try:
            from astrbot.api.message_components import Image, Plain
            self._ImageComponent = Image
            self._PlainComponent = Plain
        except ImportError:
            try:
                from astrbot.api.message import Image, Plain
                self._ImageComponent = Image
                self._PlainComponent = Plain
            except ImportError:
                logger.error("[消息防抖动] 严重: 组件导入失败")

        logger.info(f"[消息防抖动] v2.1.0 加载 | 事件驱动模式 | 防抖: {self.debounce_time}s")

    def is_command(self, message: str) -> bool:
        message = message.strip()
        if not message: return False
        for prefix in self.command_prefixes:
            if message.startswith(prefix): return True
        return False

    def _parse_message(self, message_obj) -> Tuple[str, bool, List[str]]:
        text = ""
        has_image = False
        image_urls = []
        try:
            if not hasattr(message_obj, "message"): return "", False, []
            for component in message_obj.message:
                if hasattr(component, 'text') and component.text:
                    text += component.text
                elif hasattr(component, 'content') and component.content:
                    text += component.content
                
                is_img = False
                if self._ImageComponent and isinstance(component, self._ImageComponent): is_img = True
                elif component.__class__.__name__ == 'Image': is_img = True
                
                if is_img:
                    has_image = True
                    if hasattr(component, 'url') and component.url: image_urls.append(component.url)
                    elif hasattr(component, 'file') and component.file: image_urls.append(component.file)
        except Exception:
            pass
        return text, has_image, image_urls

    def _reconstruct_event(self, event: AstrMessageEvent, text: str, image_urls: List[str]):
        """重构事件"""
        event.message_str = text
        if not self._PlainComponent: return

        chain = []
        if text:
            chain.append(self._PlainComponent(text=text))
        
        if image_urls and self._ImageComponent:
            for url in image_urls:
                try:
                    chain.append(self._ImageComponent(file=url))
                except TypeError:
                    chain.append(self._ImageComponent(url=url))
                except Exception: pass
        
        if hasattr(event.message_obj, "message"):
            try:
                event.message_obj.message = chain
            except Exception: pass

    async def _timer_coroutine(self, uid: str, duration: float):
        """计时器协程：等待指定时间后触发结算"""
        try:
            await asyncio.sleep(duration)
            # 时间到，如果没有被取消，则触发 flush_event
            if uid in self.sessions:
                self.sessions[uid]['flush_event'].set()
        except asyncio.CancelledError:
            # 任务被取消（说明有新消息来了），直接退出，不做任何事
            pass

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=50)
    async def handle_private_msg(self, event: AstrMessageEvent):
        if not self.enable_plugin or self.debounce_time <= 0: return

        # 1. 解析
        raw_text, has_image, current_urls = self._parse_message(event.message_obj)
        if not raw_text: raw_text = (event.message_str or "").strip()
        uid = event.unified_msg_origin

        # 指令 -> 立即结算当前会话
        if self.is_command(raw_text):
            if uid in self.sessions:
                # 取消计时器，立即触发结算
                if self.sessions[uid].get('timer_task'):
                    self.sessions[uid]['timer_task'].cancel()
                self.sessions[uid]['flush_event'].set()
            return

        # 忽略空消息
        if not raw_text and not has_image: return

        # ================== 核心防抖逻辑 ==================

        # 场景 A: 追加到现有会话 (Msg 2, 3...)
        if uid in self.sessions:
            session = self.sessions[uid]
            
            # 1. 追加数据
            if raw_text: session['buffer'].append(raw_text)
            if current_urls: session['images'].extend(current_urls)
            
            # 2. 重置计时器 (取消旧的，开个新的)
            if session.get('timer_task'):
                session['timer_task'].cancel()
            
            session['timer_task'] = asyncio.create_task(
                self._timer_coroutine(uid, self.debounce_time)
            )
            
            # 3. 销毁当前事件 (它已化作燃料)
            event.stop_event()
            return

        # 场景 B: 启动新会话 (Msg 1)
        
        # 1. 初始化
        flush_event = asyncio.Event()
        # 立即启动第一个计时器
        timer_task = asyncio.create_task(
            self._timer_coroutine(uid, self.debounce_time)
        )
        
        self.sessions[uid] = {
            'buffer': [raw_text] if raw_text else [],
            'images': current_urls,
            'flush_event': flush_event,
            'timer_task': timer_task
        }
        
        logger.info(f"[消息防抖动] 开始收集 - 用户: {uid}")

        # 2. 挂起等待 (零CPU消耗)
        # Msg 1 在这里暂停，直到 flush_event 被 _timer_coroutine 设置为 True
        await flush_event.wait()
        
        # ================== 结算阶段 ==================
        
        # 3. 取出数据
        if uid not in self.sessions: return # 防御性编程
        session_data = self.sessions.pop(uid)
        
        buffer = session_data['buffer']
        all_images = session_data['images']
        merged_text = self.merge_separator.join(buffer).strip()
        
        # 4. 日志与重构
        if not merged_text and not all_images: return

        img_info = f" + {len(all_images)}图" if all_images else ""
        logger.info(f"[消息防抖动] 结算触发 - 共 {len(buffer)} 条{img_info} -> 发送")
        
        self._reconstruct_event(event, merged_text, all_images)
        return