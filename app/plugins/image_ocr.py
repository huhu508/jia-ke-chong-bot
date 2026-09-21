"""群聊图片识别：收到运动记录截图后 OCR 提取数据并回显。

未绑定平台的成员：识别结果会记入当日明细（供排行）+ 累计里程。
"""

import asyncio
import base64
import hashlib
import time
from io import BytesIO
from pathlib import Path

import httpx
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, MessageEvent
from nonebot.log import logger
from nonebot.rule import Rule
from PIL import Image

from ..db import get_session
from ..models.member import Member
from ..services import checkin, cheers, llm, parsers, sync, timeutil
from ..services.member import get_or_create_member
from ..services.ocr import recognize_boxes
from ..services.providers.base import DailyStats
from .admin import notify_gift_claim


def _has_image(event) -> bool:
    """仅当消息含图片段时才触发，避免纯文本消息也空转一次 handler。"""
    return any(s.type == "image" for s in event.get_message())


image_matcher = on_message(priority=10, block=False, rule=Rule(_has_image))

# 保存收到的图片，便于排查 OCR 识别误差
IMG_DIR = Path("data/images")


async def _download(bot: Bot, seg) -> bytes:
    """下载图片字节。优先 get_image API，其次直接下载 url。"""
    file = seg.data.get("file", "")
    url = seg.data.get("url", "")
    try:
        res = await bot.get_image(file=file)
        if isinstance(res, dict):
            f = res.get("file", "")
            if isinstance(f, str) and f.startswith("base64://"):
                return base64.b64decode(f[len("base64://") :])
            if isinstance(f, str) and f and Path(f).exists():
                return Path(f).read_bytes()
            u = res.get("url")
            if u:
                async with httpx.AsyncClient(timeout=30, verify=False) as c:
                    r = await c.get(u)
                    r.raise_for_status()
                    return r.content
    except Exception as e:
        logger.warning(f"get_image 失败，尝试直接下载 url: {e}")
    if url:
        async with httpx.AsyncClient(timeout=30, verify=False) as c:
            r = await c.get(url)
            r.raise_for_status()
            return r.content
    return b""


# 表情包/缩略图的最长边阈值：QQ 表情包通常 ≤ 400px，手机运动截图 ≥ 1280px
_MIN_IMAGE_EDGE = 500


def _is_too_small(img_bytes: bytes) -> bool:
    """读图片尺寸（Pillow 惰性读 header，不加载像素），判断是否表情包/缩略图。

    读不出尺寸时返回 False（不拦截），交给后续 OCR 结果判据兜底。
    """
    try:
        with Image.open(BytesIO(img_bytes)) as im:
            w, h = im.size
        return max(w, h) < _MIN_IMAGE_EDGE
    except Exception as e:
        logger.warning(f"读取图片尺寸失败: {e}")
        return False


def _format_data(data: dict) -> list:
    # 与 query.py 一致：值 >0 才显示，避免把未识别的 0 值刷屏。
    lines = []
    if data.get("distance_km"):
        lines.append(f"📏 距离：{data['distance_km']} km")
    if data.get("avg_pace_sec_per_km"):
        lines.append(f"🏃 平均配速：{cheers.format_pace(data['avg_pace_sec_per_km'])} /km")
    if data.get("steps"):
        lines.append(f"👟 步数：{data['steps']}")
    if data.get("ascent_meters"):
        lines.append(f"⛰️ 爬升：{data['ascent_meters']:.0f} m")
    if data.get("calories"):
        lines.append(f"🔥 消耗：{data['calories']} 千卡")
    if data.get("active_minutes"):
        lines.append(f"⏱ 活动时长：{data['active_minutes']} 分钟")
    if data.get("avg_hr"):
        lines.append(f"💓 平均心率：{data['avg_hr']} bpm")
    if data.get("sleep_hours"):
        lines.append(f"😴 睡眠：{data['sleep_hours']} 小时")
    return lines


def _format_cheer(data: dict) -> str:
    """把识别结果渲染成鼓励语气回复：开场/结尾按数据上下文选取（集中在 cheers 库）。"""
    lines = _format_data(data)
    return cheers.opener(data) + "\n" + "\n".join(lines) + "\n\n" + cheers.closer(data)


# ---------------------------------------------------------------------------
# 重复打卡去重（内存态，10 分钟窗口）
# NapChat 偶发发送超时会诱导用户重发同一张图，用 MD5 短窗口去重，避免重复累计里程。
# ---------------------------------------------------------------------------

_RECENT_IMAGES: dict[str, float] = {}  # f"{qq}:{md5}" -> 记录时间戳
_DUP_WINDOW_SEC = 600


def _is_duplicate(qq: str, md5: str) -> bool:
    """同一张图在窗口期内重发视为重复；顺带惰性清理过期项。"""
    now = time.time()
    stale = [k for k, ts in _RECENT_IMAGES.items() if now - ts > _DUP_WINDOW_SEC]
    for k in stale:
        _RECENT_IMAGES.pop(k, None)
    return f"{qq}:{md5}" in _RECENT_IMAGES


def _mark_seen(qq: str, md5: str) -> None:
    _RECENT_IMAGES[f"{qq}:{md5}"] = time.time()


@image_matcher.handle()
async def handle_image(bot: Bot, event: MessageEvent):
    segs = [s for s in event.get_message() if s.type == "image"]
    if not segs:
        return

    qq = event.get_user_id()
    name = getattr(getattr(event, "sender", None), "nickname", None) or qq
    img_bytes = await _download(bot, segs[0])
    if not img_bytes:
        logger.warning(f"[图片识别] qq={qq} 图片下载失败，静默跳过")
        return

    # 表情包/缩略图：尺寸过小，直接跳过（不 OCR、不落盘、不响应）
    if _is_too_small(img_bytes):
        logger.info(f"[图片识别] qq={qq} 疑似表情包/小图，跳过")
        return

    # 图片指纹：用于短窗口去重（见 _is_duplicate）
    img_md5 = hashlib.md5(img_bytes).hexdigest()

    # 保存原始图片，便于排查 OCR 识别误差
    try:
        IMG_DIR.mkdir(parents=True, exist_ok=True)
        (IMG_DIR / f"{time.strftime('%Y%m%d_%H%M%S')}_{qq}.jpg").write_bytes(img_bytes)
    except Exception as e:
        logger.warning(f"保存图片失败: {e}")

    try:
        result = await asyncio.to_thread(recognize_boxes, img_bytes)
    except Exception as e:
        logger.warning(f"[图片识别] qq={qq} OCR 识别失败: {e}")
        return

    # 解析兜底：解析器异常不应拖垮整条 handler，静默跳过即可
    try:
        text = "\n".join(str(item[1]) for item in result)
        # 主路径：按框坐标做「标签—数值」空间关联；无结果时退回纯文本解析
        data = parsers.parse_activity_from_boxes(result)
        if not data:
            data = parsers.parse_activity(text)
        # 多模态兜底：本地 OCR 一个字段都没认出来时，用视觉模型再看一次；
        # 失败返回 None → 保持空 dict，照常走后续「未识别出数据，静默跳过」
        if not data:
            data = await asyncio.to_thread(llm.vision_extract, img_bytes) or {}
            if data:
                logger.info(f"[图片识别] qq={qq} 本地 OCR 未命中，视觉兜底结果: {data}")
    except Exception as e:
        logger.warning(f"[图片识别] qq={qq} 解析失败: {e}")
        return
    logger.info(f"[图片识别] qq={qq}\nOCR 原文:\n{text}\n解析结果: {data}")

    # 页面类型分析：统计/列表页（月汇总、活动列表等）展示的是多条运动的聚合，
    # 不是单次运动详情，不应作为一次运动回显/落库。先分析再确认，避免误读。
    page_kind = parsers.detect_page_kind(text)
    if page_kind == "summary":
        logger.info(f"[图片识别] qq={qq} 识别为统计/列表页，拒绝记录")
        await image_matcher.finish(
            "🔍 我识别出这是一张「统计/列表」页（多条运动的汇总），不是单次运动详情，所以先不记录。\n"
            "· 若要记录某一次运动：点开那条记录，发**单次运动详情页**截图（含这一次的距离/时长/配速）；\n"
            "· 若要查自己的周/月汇总：直接发「周数据」「月数据」，我会按平台接口自动统计。"
        )

    # 合理性校验：OCR 偶发把步数 / 卡路里 / 统计页总量误识别成距离，落库前先清洗+拦截，
    # 超范围的夸张跑量直接拒绝，避免污染排行与累计里程（这是 detect_page_kind 之外的最后防线）。
    data, reject_reason = parsers.sanitize_activity(data)
    if reject_reason:
        logger.info(f"[图片识别] qq={qq} 数据不合理，拒绝记录: {reject_reason}")
        await image_matcher.finish(
            f"⚠️ {reject_reason}，本次未记录。\n"
            "若是单次运动详情，请重发一张更清晰的截图；若是多天汇总，直接发「周数据 / 月数据」查看。"
        )

    if not any(
        k in data
        for k in (
            "distance_km",
            "steps",
            "calories",
            "active_minutes",
            "sleep_hours",
            "ascent_meters",
            "avg_pace_sec_per_km",
            "avg_hr",
        )
    ):
        logger.info(f"[图片识别] qq={qq} 未识别出运动数据，静默跳过")
        return

    # 已绑定平台 → 只展示不写库（数据已由平台接口自动同步）；
    # 未绑定平台 → 累加累计里程 + 写当日明细（供排行，含爬升/配速等）。
    session = get_session()
    try:
        member = session.get(Member, qq)
        bound = member is not None and bool(member.platform)
        if member is not None:
            name = member.display_name
    finally:
        session.close()

    if bound:
        # 已绑定平台：截图仅回显不写库（数据已由平台接口自动同步），并明确告知
        # 「以 App 为准」，避免用户误以为截图又被记了一次、结果「今日」却没变化。
        await image_matcher.finish(
            _format_cheer(data)
            + "\n\n📌 你已绑定平台，本次截图未计入数据，今日与排行均以 App 同步为准"
        )
    if "distance_km" not in data:
        await image_matcher.finish(_format_cheer(data))

    # 重复打卡去重：同一张图短时间内重发，不重复累计
    if _is_duplicate(qq, img_md5):
        await image_matcher.finish(
            _format_cheer(data) + "\n\n⏳ 这张截图刚才已经记过啦，本次不重复累计"
        )

    # 写库：SQLite 写入极快，直接在事件循环内同步执行，避免把同一个 session 传进
    # asyncio.to_thread（不同线程共享 Session 违反 SQLAlchemy 线程安全约定）。
    milestone_hits: list[int] = []
    fest = None
    festival_km = 0.0
    gift_rank: int | None = None
    lottery_result: tuple[int, list[str]] | None = None
    session = get_session()
    try:
        member = get_or_create_member(session, qq, name)
        session.commit()
        total = sync.add_manual_distance(member, data["distance_km"], session)
        # 截图记录也进当日明细，让未绑定成员出现在每日排行里。
        # parsers 返回 dict，统一转成 DailyStats（过滤非模型字段，防御未来新增键）。
        today_d = timeutil.today()
        stats = DailyStats(
            date=today_d,
            **{k: v for k, v in data.items() if k in DailyStats.model_fields},
        )
        sync.record_manual_activity(member, today_d, stats, session)
        sync.log_checkin(qq, today_d, data["distance_km"], session)
        _mark_seen(qq, img_md5)

        # 打卡彩蛋：里程碑 / 礼物 / 节日+特殊距离 / 群抽奖（幂等，状态表保证只触发一次）
        checkin_total = checkin.total_days(qq, session)
        milestone_hits = checkin.crossed_milestones(qq, checkin_total, session)
        gift_rank = checkin.auto_gift(qq, checkin_total, session)
        fest = checkin.match_festival(today_d, data.get("distance_km", 0.0))
        festival_km = data.get("distance_km", 0.0)
        lt = checkin.check_lottery(session)
        if lt is not None:
            winners = checkin.draw_lottery(session)
            winner_names = []
            for w in winners:
                m = session.get(Member, w)
                winner_names.append(m.display_name if m else w)
            lottery_result = (lt, winner_names)
        session.commit()
    except Exception as e:
        logger.exception(f"[图片识别] qq={qq} 记录失败: {e}")
        await image_matcher.finish(f"记录失败，请稍后重试：{e}")
    finally:
        session.close()

    reply = (
        _format_cheer(data)
        + f"\n\n✅ 已记入今日数据：本次 +{data['distance_km']} km，累计 {total} km，发「今日」即可查看"
    )
    # 里程碑彩蛋：AI 一句祝贺，失败降级模板
    for m in milestone_hits:
        cheer = await asyncio.to_thread(llm.milestone_cheer, name, m)
        reply += f"\n\n🎉 {cheer or f'达成第 {m} 次打卡里程碑，坚持就是胜利！'}"
    # 第 GIFT_DAYS 天礼物：自动领取（先到先得），并通知团长
    if gift_rank is not None:
        reply += (
            f"\n\n🎁 恭喜 {name}！你是第 {gift_rank} 位达成第 {checkin.GIFT_DAYS} 天打卡的跑友，"
            "自动领取「小红书惊喜小礼物」，已通知团长安排发货～"
        )
        await notify_gift_claim(bot, qq, name, gift_rank, getattr(event, "group_id", None))
    # 节日+特殊距离彩蛋
    if fest is not None:
        fallback = f"{fest['name']}快乐！{festival_km} km 跑得漂亮，继续加油～"
        cheer = await asyncio.to_thread(llm.festival_cheer, name, fest["name"], festival_km)
        reply += f"\n\n🎊 {cheer or fallback}"
    # 大模型补一句点评：失败返回 None，自动降级为纯数据回显，不影响主链路
    comment = await asyncio.to_thread(llm.comment_checkin, name, data)
    if comment:
        reply += f"\n\n💬 {comment}"

    # 群抽奖开奖：公告到当前群（状态表保证只触发一次）
    if lottery_result is not None:
        lt, winners = lottery_result
        announce = (
            f"🎉 全群累计打卡突破 {lt} 天！群抽奖开奖：{'、'.join(winners)} 获得{checkin.LOTTERY_PRIZE}～"
            f"恭喜这位跑友！"
        )
        gid = getattr(event, "group_id", None)
        if gid is not None:
            try:
                await bot.send_group_msg(group_id=gid, message=announce)
            except Exception as e:
                logger.warning(f"群抽奖公告发送失败: {e}")

    await image_matcher.finish(reply)
