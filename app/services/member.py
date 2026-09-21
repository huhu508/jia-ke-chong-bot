"""成员统一读写：按 QQ 号定位，昵称统一为 QQ 昵称。"""

from sqlalchemy.orm import Session

from ..models.member import INVALID_NICKNAMES, Member


def normalize_nickname(nickname: str | None) -> str:
    """清洗昵称：去首尾空白；无效占位（如群临时会话拿不到真名的「临时会话」）返回空串。"""
    n = (nickname or "").strip()
    return "" if n.lower() in INVALID_NICKNAMES else n


def get_or_create_member(session: Session, qq: str, nickname: str | None = None) -> Member:
    """按 QQ 号取成员，不存在则新建；已存在且给了新昵称则更新。

    QQ 号是 Member 主键，唯一性由数据库保证。昵称统一取 QQ 昵称
    （event.sender.nickname），保证绑定 / 截图 / 榜单 / 今日 多处显示一致。
    无效占位（「临时会话」等）会被清洗：新建时退回 QQ 号，已有成员则保留旧昵称不覆盖。
    """
    clean = normalize_nickname(nickname)
    member = session.get(Member, qq)
    if member is None:
        member = Member(qq=qq, nickname=clean or qq)
        session.add(member)
    elif clean and member.nickname != clean:
        member.nickname = clean
    return member
