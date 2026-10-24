"""邀请码运维工具（PRD 3.5，Q3=A：先做命令行，不做管理页）。

用法：

    python scripts/invite_tool.py new --count 3 --label "10月批次" --max-uses 1   # 生成（明文只显示这一次）
    python scripts/invite_tool.py list                              # 表格：hint/备注/状态/绑定给谁/时间
    python scripts/invite_tool.py disable --hint ab12               # 停用（或 --id <邀请码 id>）

为什么明文只显示一次：Q1=C 语义下邀请码**就是登录凭据**，库内只存 sha256(码)，
所以泄露即等于账号被拿。生成时没记下来，只能停用重发——这是刻意的取舍。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.dao import invites as invites_dao  # noqa: E402


STATUS_LABELS = {"pending": "未使用", "bound": "已绑定", "disabled": "已停用"}


def _cmd_new(args: argparse.Namespace) -> int:
    codes = invites_dao.generate_invites(
        args.count,
        args.label,
        args.by,
        expires_at=args.expires_at,
        max_uses=args.max_uses,
    )
    print(f"已生成 {len(codes)} 个邀请码（备注：{args.label or '无'}）。")
    print("！ 明文只显示这一次，请立刻复制保存；库内只有哈希，之后无法再查看。")
    for code in codes:
        print(f"  {code}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    del args  # 无参数
    rows = invites_dao.list_invites()
    if not rows:
        print("还没有发出任何邀请码。用 `python scripts/invite_tool.py new --count N` 生成。")
        return 0
    header = ("hint", "备注", "状态", "已用/上限", "有效期", "绑定给谁", "绑定时间", "创建时间")
    table = [
        (
            f"****{row['code_hint']}",
            row.get("label") or "-",
            STATUS_LABELS.get(str(row["status"]), str(row["status"])),
            f"{row.get('used_count', 0)}/{row.get('max_uses', 1)}",
            row.get("expires_at") or "永不过期",
            row.get("bound_username") or row.get("bound_user_id") or "-",
            row.get("bound_at") or "-",
            row.get("created_at") or "-",
        )
        for row in rows
    ]
    widths = [max(len(header[i]), *(len(row[i]) for row in table)) for i in range(len(header))]
    print("  ".join(header[i].ljust(widths[i]) for i in range(len(header))))
    print("  ".join("-" * widths[i] for i in range(len(header))))
    for row in table:
        print("  ".join(row[i].ljust(widths[i]) for i in range(len(header))))
    counts = invites_dao.count_by_status()
    print()
    print(
        f"合计 {len(rows)} 个：未使用 {counts['pending']} / 已绑定 {counts['bound']} / 已停用 {counts['disabled']}"
    )
    print("（明文无法回显：库内只存 sha256(码)，这是 C 语义的安全要求。）")
    return 0


def _cmd_disable(args: argparse.Namespace) -> int:
    try:
        row = invites_dao.disable_invite(invite_id=args.id, hint=args.hint)
    except ValueError as exc:
        print(f"停用失败：{exc}")
        return 2
    if row is None:
        print("没找到对应的邀请码，请检查 --id / --hint 后重试。")
        return 1
    label = row.get("label") or "无"
    status = STATUS_LABELS.get(str(row["status"]), str(row["status"]))
    print(f"已停用 ****{row['code_hint']}（备注：{label}，当前状态：{status}）。")
    if row.get("bound_user_id"):
        print("该码曾绑定账号：停用后该账号仍可用「用户名+密码」登录（停用码不等于封号）。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="invite_tool.py",
        description="NovelCraft 邀请码工具：生成 / 列表 / 停用（明文只在生成时显示一次）",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    new_parser = sub.add_parser("new", help="生成新的邀请码（明文只显示这一次）")
    new_parser.add_argument("--count", type=int, default=1, help="生成个数，1–100，默认 1")
    new_parser.add_argument("--label", default="", help="备注，例如批次或给谁（如：10月批次）")
    new_parser.add_argument("--by", default="", help="操作者标识，写进 created_by 便于留痕（可选）")
    new_parser.add_argument(
        "--expires-at",
        default="",
        help="失效时间（带时区的 ISO-8601，例如 2030-01-01T00:00:00Z；默认永不过期）",
    )
    new_parser.add_argument(
        "--max-uses",
        type=int,
        default=1,
        help=f"最多注册使用次数，1–{invites_dao.MAX_USES}，默认 1",
    )
    new_parser.set_defaults(handler=_cmd_new)
    list_parser = sub.add_parser("list", help="列出已发出的邀请码（含状态与绑定账号，不含明文）")
    list_parser.set_defaults(handler=_cmd_list)
    disable_parser = sub.add_parser("disable", help="停用邀请码（用 --hint 或 --id 指定）")
    disable_parser.add_argument("--id", default="", help="邀请码 id（可用 SQLite 直接查；一般用 --hint）")
    disable_parser.add_argument("--hint", default="", help="邀请码后 4 位，例如 ab12")
    disable_parser.set_defaults(handler=_cmd_disable)
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except ValueError as exc:
        print(f"参数错误：{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
