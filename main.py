import asyncio
from typing import List, Tuple, Dict, Optional
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import AstrBotConfig, logger

@register(
    "continuous_message",
    "aliveriver",
    "将用户短时间内发送的多条私聊消息合并成一条发送给LLM（仅私聊模式）",
    "2.0.0"
)
class ContinuousMessagePlugin(Star):
    """
    消息防抖动插件 v2.0.0
    消息防抖动插件（仅私聊模式）
    
    功能：
    1. 拦截用户短时间内发送的多条私聊消息
    2. 在防抖时间结束后，将这些消息合并成一条发送给LLM
    3. 过滤指令消息，不参与合并
    4. 保持人格设定和对话历史
    5. 支持图片识别和传递

    安全设计：
    - 强制仅在私聊启用，避免群聊中不同用户的消息被误合并
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

        logger.info(f"[消息防抖动] v2.0.0 加载 | 事件驱动模式 | 防抖: {self.debounce_time}s")

    def is_command(self, message: str) -> bool:
        message = message.strip()
        if not message: return False
        for prefix in self.command_prefixes:
            if message.startswith(prefix): return True
        return False

    def _parse_message(self, message_obj) -> Tuple[str, bool, List[str]]:
        """
        解析消息对象，提取文本和图片信息
        
        Returns:
            (文本内容, 是否包含图片, 图片URL列表)
        """
        text = ""
        has_image = False
        image_urls = []
        try:
            if not hasattr(message_obj, "message"): return "", False, []
            
            # 遍历消息组件，提取文本和图片
            for component in message_obj.message:
                # 提取文本内容（支持多种属性名）
                if hasattr(component, 'text') and component.text:
                    text += component.text
                elif hasattr(component, 'content') and component.content:
                    text += component.content
                
                # 识别图片组件（优先使用 isinstance，后备使用类名检查）
                is_img = False
                if self._ImageComponent and isinstance(component, self._ImageComponent): is_img = True
                elif component.__class__.__name__ == 'Image': is_img = True
                
                # 提取图片URL（支持 url 或 file 属性）
                if is_img:
                    has_image = True
                    if hasattr(component, 'url') and component.url: image_urls.append(component.url)
                    elif hasattr(component, 'file') and component.file: image_urls.append(component.file)
        except Exception:
            pass
        return text, has_image, image_urls

    def _reconstruct_event(self, event: AstrMessageEvent, text: str, image_urls: List[str]):
        """
        重构消息事件，将合并后的文本和图片重新组装到事件对象中
        这样事件可以继续传播给后续的插件/框架处理
        """
        event.message_str = text
        if not self._PlainComponent: return

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
                    # 如果 file 参数不支持，尝试 url 参数
                    chain.append(self._ImageComponent(url=url))
                except Exception: pass
        
        # 更新事件的消息对象
        if hasattr(event.message_obj, "message"):
            try:
                event.message_obj.message = chain
            except Exception: pass

    async def _timer_coroutine(self, uid: str, duration: float):
        """
        计时器协程：等待指定时间后触发结算
        
        当有新消息到来时，旧计时器会被取消（CancelledError），
        新消息会创建新的计时器重新开始倒计时。
        """
        try:
            await asyncio.sleep(duration)
            # 时间到且未被取消，触发结算事件（唤醒等待的主协程）
            if uid in self.sessions:
                self.sessions[uid]['flush_event'].set()
        except asyncio.CancelledError:
            # 任务被取消（说明有新消息到来，计时器需要重置）
            # 直接退出即可，新消息会创建新的计时器
            pass

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=50)
    async def handle_private_msg(self, event: AstrMessageEvent):
        if not self.enable_plugin or self.debounce_time <= 0: return

        # 1. 解析消息内容
        raw_text, has_image, current_urls = self._parse_message(event.message_obj)
        if not raw_text: raw_text = (event.message_str or "").strip()
        uid = event.unified_msg_origin

        # 2. 处理指令消息：立即中断当前防抖会话并结算
        # 指令消息本身不会参与防抖，会正常传播执行
        if self.is_command(raw_text):
            if uid in self.sessions:
                # 取消计时器，立即触发结算（已收集的消息会先发送）
                if self.sessions[uid].get('timer_task'):
                    self.sessions[uid]['timer_task'].cancel()
                self.sessions[uid]['flush_event'].set()
            return

        # 3. 忽略空消息（既无文本也无图片）
        if not raw_text and not has_image: return

        # ================== 核心防抖逻辑 ==================

        # 场景 A: 追加到现有会话 (Msg 2, 3...)
        # 用户已有活跃的防抖会话，将新消息追加到缓冲区
        if uid in self.sessions:
            session = self.sessions[uid]
            
            # 1. 追加数据到缓冲区
            if raw_text: session['buffer'].append(raw_text)
            if current_urls: session['images'].extend(current_urls)
            
            # 2. 重置计时器：取消旧任务，创建新任务（重新开始倒计时）
            if session.get('timer_task'):
                session['timer_task'].cancel()
            
            session['timer_task'] = asyncio.create_task(
                self._timer_coroutine(uid, self.debounce_time)
            )
            
            # 3. 阻止当前事件继续传播（消息内容已加入缓冲区，无需单独处理）
            event.stop_event()
            return

        # 场景 B: 启动新会话 (Msg 1)
        # 这是用户的第一条消息，需要创建新的防抖会话并等待
        
        # 1. 初始化会话数据
        flush_event = asyncio.Event()  # 用于唤醒等待的协程
        timer_task = asyncio.create_task(
            self._timer_coroutine(uid, self.debounce_time)
        )
        
        self.sessions[uid] = {
            'buffer': [raw_text] if raw_text else [],  # 文本消息缓冲区
            'images': current_urls,                    # 图片URL列表
            'flush_event': flush_event,                # 结算触发事件
            'timer_task': timer_task                   # 当前计时器任务
        }
        
        logger.info(f"[消息防抖动] 开始收集 - 用户: {uid}")

        # 2. 挂起主协程，等待结算触发
        # 当计时器超时或收到指令时，_timer_coroutine 会调用 flush_event.set() 唤醒这里
        await flush_event.wait()
        
        # ================== 结算阶段 ==================
        # 计时器超时或被指令中断，开始合并消息并重构事件
        
        # 3. 从会话存储中取出所有缓冲数据
        if uid not in self.sessions: return  # 防御性检查（理论上不应发生）
        session_data = self.sessions.pop(uid)  # 取出并删除会话（避免重复处理）
        
        buffer = session_data['buffer']
        all_images = session_data['images']
        merged_text = self.merge_separator.join(buffer).strip()
        
        # 4. 合并消息并重构事件对象
        if not merged_text and not all_images: return  # 空数据直接返回

        img_info = f" + {len(all_images)}图" if all_images else ""
        logger.info(f"[消息防抖动] 结算触发 - 共 {len(buffer)} 条{img_info} -> 发送")
        
        # 重构事件：将合并后的文本和图片重新组装到事件中
        # 重构后的事件会继续传播给后续插件/框架，由它们处理 LLM 调用
        self._reconstruct_event(event, merged_text, all_images)
        return