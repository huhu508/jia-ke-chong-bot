"""成员统一读写：按 QQ 号定位，昵称统一为 QQ 昵称。"""

from sqlalchemy.orm import Session

from ..models.member import Member


def get_or_create_member(session: Session, qq: str, nickname: str | None = None) -> Member:
    """按 QQ 号取成员，不存在则新建；已存在且给了新昵称则更新。

    QQ 号是 Member 主键，唯一性由数据库保证。昵称统一取 QQ 昵称
    （event.sender.nickname），保证绑定 / 截图 / 榜单 / 今日 多处显示一致。
    """
    member = session.get(Member, qq)
    if member is None:
        member = Member(qq=qq, nickname=nickname or qq)
        session.add(member)
    elif nickname and member.nickname != nickname:
        member.nickname = nickname
    return member
