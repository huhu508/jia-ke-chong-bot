"""管理员补录截图：把今天群里发的、尚未记录的运动截图重新识别入账。

场景：机器人离线/死机期间群友发的截图没被处理。管理员在群里发「补录截图」，
机器人通过 get_group_msg_history 拉取今天该群全部消息里的图片，跳过已记录过的
（data/images 落盘 MD5 + image_ocr 内存去重），按正常截图打卡链路补录（识别 →
视觉兜底 → 清洗 → 页面类型 → 落库 + 打卡彩蛋状态），最后回一条汇总。

命令隐藏（不进帮助菜单），仅超级管理员可用。
"""

import asyncio
import hashlib
import time
from datetime import datetime
from datetime import time as dtime

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment
from nonebot.log import logger

from ..db import get_session
from ..models.member import Member
from ..services import checkin, parsers, sync, timeutil
from ..services.llm import vision_extract, vision_reconcile
from ..services.member import get_or_create_member
from ..services.ocr import recognize_boxes
from ..services.permission import is_superuser
from ..services.providers.base import DailyStats
from .admin import notify_gift_claim
from .image_ocr import (
    _MAX_IMAGE_BYTES,
    IMG_DIR,
    _cleanup_images,
    _dhash,
    _download,
    _is_duplicate,
    _is_too_small,
    _mark_seen,
)

replay_cmd = on_command("补录截图", aliases={"补录今日截图"}, priority=5, block=True)

# 拉取历史消息的最大翻页次数（安全上限，正常一天的消息几页就够）
_MAX_PAGES = 1000


def _today_start_ts() -> float:
    """今天 0 点（北京时间）的 epoch 秒，用于过滤历史消息。"""
    return datetime.combine(timeutil.today(), dtime.min, tzinfo=timeutil.CHINA_TZ).timestamp()


def _existing_md5s() -> set[str]:
    """data/images 已落盘图片的 MD5 集合（= 今天已被正常链路处理过的图）。

    机器人重启后内存去重表清空，落盘文件是「今天已记录」的唯一完整依据；
    与内存去重（重启后新记录的）叠加，避免重复累计。
    """
    out: set[str] = set()
    try:
        for p in IMG_DIR.glob("*.jpg"):
            out.add(hashlib.md5(p.read_bytes()).hexdigest())
    except Exception as e:
        logger.warning(f"[补录截图] 读取落盘图片 MD5 失败: {e}")
    return out


async def _fetch_today_images(bot: Bot, group_id: int) -> list[tuple[str, dict]]:
    """拉取今天群里的全部图片消息，返回 [(sender_qq, image_segment), ...]（按时间升序）。"""
    start_ts = _today_start_ts()
    seen_msgs: dict[int, dict] = {}
    seq = 0
    for _ in range(_MAX_PAGES):
        resp = await bot.call_api("get_group_msg_history", group_id=group_id, message_seq=seq)
        batch = resp.get("messages") or []
        if not batch:
            break
        for m in batch:
            mid = m.get("message_id")
            if mid is not None and mid not in seen_msgs:
                seen_msgs[mid] = m
        oldest_time = min((m.get("time", 0) for m in batch), default=0)
        if oldest_time and oldest_time < start_ts:
            break  # 这一页已经翻到昨天，不再往前
        oldest_seq = min((m.get("message_seq", 0) for m in batch), default=0)
        if oldest_seq == seq:
            break  # 序号没有推进，防死循环
        seq = oldest_seq

    out: list[tuple[str, dict]] = []
    for m in sorted(seen_msgs.values(), key=lambda m: (m.get("message_seq", 0), m.get("time", 0))):
        if m.get("time", 0) < start_ts:
            continue
        sender = m.get("sender") or {}
        qq = str(sender.get("user_id", ""))
        msg = m.get("message")
        if not isinstance(msg, list):
            continue
        for seg in msg:
            if isinstance(seg, dict) and seg.get("type") == "image":
                out.append((qq, seg))
    return out


async def _process_one(bot: Bot, group_id: int, qq: str, seg: dict, md5s: set[str]) -> str:
    """补录一张图，返回结果标签（recorded / dup / bound / summary / unrecognized / download / reject）。"""
    # 历史消息里的图片段是纯 dict（seg["data"]["file"]），_download 期望 MessageSegment
    # 对象（seg.data.get(...)），这里转一道，复用与正常链路完全相同的下载逻辑。
    seg_obj = MessageSegment(type="image", data=seg.get("data") or {})
    img_bytes = await _download(bot, seg_obj)
    if not img_bytes:
        return "download"
    if len(img_bytes) > _MAX_IMAGE_BYTES or _is_too_small(img_bytes):
        return "download"

    md5 = hashlib.md5(img_bytes).hexdigest()
    dhash = _dhash(img_bytes)
    if _is_duplicate(md5, dhash) or md5 in md5s:
        return "dup"

    session = get_session()
    try:
        member = session.get(Member, qq)
        bound = member is not None and bool(member.platform)
        name = member.display_name if member is not None else qq
    finally:
        session.close()

    # 与正常链路一致：OCR 主路径 → 视觉兜底（读不出距离 / 结果可疑时）→ 清洗 → 页面类型
    try:
        result = await asyncio.to_thread(recognize_boxes, img_bytes)
    except Exception as e:
        logger.warning(f"[补录截图] qq={qq} OCR 失败: {e}")
        return "unrecognized"
    text = "\n".join(str(item[1]) for item in result)
    ocr_data = parsers.parse_activity_from_boxes(result)
    if not ocr_data:
        ocr_data = parsers.parse_activity(text)
    data = dict(ocr_data)

    vision_data: dict = {}
    vision_used = False
    if not data.get("distance_km") and not bound:
        try:
            vision_data = await asyncio.to_thread(vision_extract, img_bytes) or {}
        except Exception as e:
            logger.warning(f"[补录截图] qq={qq} 视觉识别失败: {e}")
        vision_used = True
    for k, v in vision_data.items():
        if k not in data or not data.get(k):
            data[k] = v
    conflicts = parsers.find_field_conflicts(ocr_data, vision_data)
    if conflicts:
        recon = (
            await asyncio.to_thread(vision_reconcile, img_bytes, ocr_data, vision_data, conflicts)
            or {}
        )
        for k in conflicts:
            if k in recon:
                data[k] = recon[k]

    signals = parsers.suspicious_signals(data)
    if signals and not vision_used and not bound:
        fallback = await asyncio.to_thread(vision_extract, img_bytes) or {}
        if fallback:
            for k, v in fallback.items():
                if k not in data or not data.get(k):
                    data[k] = v
            conflicts = parsers.find_field_conflicts(data, fallback)
            if conflicts:
                recon = (
                    await asyncio.to_thread(vision_reconcile, img_bytes, data, fallback, conflicts)
                    or {}
                )
                for k in conflicts:
                    if k in recon:
                        data[k] = recon[k]

    data, reject_reason = parsers.sanitize_activity(data)
    if reject_reason:
        return "reject"
    if parsers.detect_page_kind(text) == "summary":
        return "summary"
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
        return "unrecognized"
    if bound:
        return "bound"

    # 落盘（与正常链路同目录，供「已记录」去重与后续排查）
    try:
        IMG_DIR.mkdir(parents=True, exist_ok=True)
        (IMG_DIR / f"{time.strftime('%Y%m%d_%H%M%S')}_{qq}.jpg").write_bytes(img_bytes)
        _cleanup_images()
    except Exception as e:
        logger.warning(f"[补录截图] 保存图片失败: {e}")

    session = get_session()
    try:
        member = get_or_create_member(session, qq, name)
        total = sync.add_manual_distance(member, data["distance_km"], session)
        today_d = timeutil.today()
        stats = DailyStats(
            date=today_d,
            **{k: v for k, v in data.items() if k in DailyStats.model_fields},
        )
        sync.record_manual_activity(member, today_d, stats, session)
        sync.log_checkin(
            qq,
            today_d,
            data["distance_km"],
            session,
            ascent_meters=data.get("ascent_meters", 0.0),
            calories=data.get("calories", 0),
            active_minutes=data.get("active_minutes", 0),
        )
        checkin_total = checkin.total_days(qq, session)
        checkin.crossed_milestones(qq, checkin_total, session)
        gift_rank = checkin.auto_gift(qq, checkin_total, session)
        session.commit()
        _mark_seen(md5, dhash)
        if gift_rank is not None:
            await notify_gift_claim(bot, qq, name, gift_rank, group_id)
        return f"recorded:{name}:{data['distance_km']:.2f}:{total:.2f}"
    except Exception as e:
        logger.exception(f"[补录截图] qq={qq} 落库失败: {e}")
        return "reject"
    finally:
        session.close()


@replay_cmd.handle()
async def handle_replay(bot: Bot, event: GroupMessageEvent):
    if not is_superuser(event):
        await replay_cmd.finish("此命令仅管理员可用")
    gid = event.group_id
    logger.info(f"[补录截图] 管理员 {event.get_user_id()} 在群 {gid} 触发补录")

    try:
        images = await _fetch_today_images(bot, gid)
    except Exception as e:
        logger.exception(f"[补录截图] 拉取群历史失败: {e}")
        await replay_cmd.finish(f"拉取群历史失败：{e}")

    if not images:
        await replay_cmd.finish("今天群里没有图片消息，无需补录")

    md5s = await asyncio.to_thread(_existing_md5s)
    counts: dict[str, int] = {}
    recorded: list[str] = []
    for qq, seg in images:
        try:
            tag = await _process_one(bot, gid, qq, seg, md5s)
        except Exception as e:
            # 单张处理失败不拖垮整批补录（如个别图片下载/解析异常），计入 error 继续下一张
            logger.warning(f"[补录截图] qq={qq} 处理异常，跳过: {e}")
            tag = "error"
        if tag.startswith("recorded:"):
            _, name, km, total = tag.split(":", 3)
            recorded.append(f"{name} +{km} km（累计 {total} km）")
            tag = "recorded"
        counts[tag] = counts.get(tag, 0) + 1

    lines = [f"📥 今日截图补录完成：共拉取 {len(images)} 张图片"]
    n_rec = len(recorded)
    if n_rec:
        lines.append(f"✅ 已记录 {n_rec} 张：")
        lines.extend(f"· {r}" for r in recorded)
    else:
        lines.append("✅ 没有需要补录的记录")
    skipped = [
        f"跳过 {counts.get('dup', 0)} 张重复",
        f"{counts.get('bound', 0)} 张已绑定成员",
        f"{counts.get('summary', 0)} 张统计/列表页",
        f"{counts.get('unrecognized', 0)} 张未识别",
        f"{counts.get('download', 0)} 张下载失败/小图",
        f"{counts.get('reject', 0)} 张数据不合理",
        f"{counts.get('error', 0)} 张处理异常",
    ]
    lines.append("⏭ " + "、".join(skipped))
    logger.info(f"[补录截图] 群 {gid} 完成: {counts}")
    await replay_cmd.finish("\n".join(lines))
